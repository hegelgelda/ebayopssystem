"""eBay Seller Hub の注文レポートCSV（Reports → Downloads → Orders）を取り込み、注文を自動で記録する。

API なしで「落札・購入の記録」「支払の記録」を手入力しないための仕組み。
- 1行＝1つの購入（複数商品をまとめ買いした注文は、先頭に合計行、続けて商品ごとの行が並ぶ）
- 商品番号（Item Number）→ 出品、なければ Custom Label（SKU）→ 在庫 → 出品 の順で照合
- 同じレポートを何度取り込んでも二重登録しない（注文番号で判定）。支払日が後から入れば支払を記録する
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit
from app.models import Item, Listing, ListingFormat as LF, Order, OrderStatus as OS, User
from app.services import notify, orders as order_svc
from app.services.errors import ServiceError
from app.timeutil import jst_to_utc_naive

# 列名（小文字・英数字だけにして比較）。eBay のレポートは版によって表記が少し違うので候補を複数持つ
COLUMNS: dict[str, list[str]] = {
    "order_number": ["ordernumber", "orderid"],
    "item_number": ["itemnumber", "itemid"],
    "sku": ["customlabel", "sku"],
    "title": ["itemtitle", "title"],
    "buyer": ["buyerusername", "buyeruserid", "userid"],
    "quantity": ["quantity"],
    "sold_for": ["soldfor", "saleprice", "price"],
    "shipping": ["shippingandhandling", "shipping"],
    "ship_country": ["shiptocountry", "shippingcountry", "buyercountry"],
    "sale_date": ["saledate", "orderdate"],
    "paid_date": ["paidondate", "paiddate", "paymentdate"],
    "ship_by": ["shipbydate"],
}

COUNTRIES = {
    "united states": "US", "usa": "US", "united kingdom": "GB", "great britain": "GB", "uk": "GB",
    "canada": "CA", "australia": "AU", "new zealand": "NZ", "germany": "DE", "france": "FR", "italy": "IT",
    "spain": "ES", "netherlands": "NL", "the netherlands": "NL", "belgium": "BE", "luxembourg": "LU",
    "switzerland": "CH", "austria": "AT", "ireland": "IE", "portugal": "PT", "sweden": "SE", "norway": "NO",
    "denmark": "DK", "finland": "FI", "iceland": "IS", "poland": "PL", "czech republic": "CZ", "czechia": "CZ",
    "slovakia": "SK", "hungary": "HU", "romania": "RO", "bulgaria": "BG", "greece": "GR", "croatia": "HR",
    "slovenia": "SI", "estonia": "EE", "latvia": "LV", "lithuania": "LT", "malta": "MT", "cyprus": "CY",
    "japan": "JP", "hong kong": "HK", "taiwan": "TW", "south korea": "KR", "korea, south": "KR",
    "republic of korea": "KR", "korea": "KR", "china": "CN", "singapore": "SG", "malaysia": "MY",
    "thailand": "TH", "philippines": "PH", "indonesia": "ID", "vietnam": "VN", "india": "IN",
    "israel": "IL", "united arab emirates": "AE", "saudi arabia": "SA", "qatar": "QA", "kuwait": "KW",
    "turkey": "TR", "türkiye": "TR", "mexico": "MX", "brazil": "BR", "chile": "CL", "argentina": "AR",
    "colombia": "CO", "peru": "PE", "puerto rico": "PR", "south africa": "ZA", "macau": "MO", "macao": "MO",
}


def _norm(h: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (h or "").lower())


def _money(s: str) -> Decimal | None:
    s = re.sub(r"[^0-9.\-]", "", (s or "").replace(",", ""))
    if not s or s in ("-", "."):
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def _currency(s: str) -> str:
    s = (s or "").upper()
    for code in ("USD", "GBP", "EUR", "AUD", "CAD", "JPY"):
        if code in s:
            return code
    if "£" in s:
        return "GBP"
    if "€" in s:
        return "EUR"
    if "¥" in s or "円" in s:
        return "JPY"
    return "USD"


_DATE_FORMATS = ("%b-%d-%y", "%b-%d-%Y", "%b %d, %Y", "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d-%b-%y",
                 "%Y/%m/%d")


def _date(s: str) -> date | None:
    s = (s or "").strip()
    if not s:
        return None
    s = re.sub(r"\s+(PST|PDT|GMT|UTC|JST)$", "", s, flags=re.I)
    s = s.split("T")[0]
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s.split(" ")[0] if fmt.count(" ") == 0 else s, fmt).date()
        except ValueError:
            continue
    return None


def _when(d: date | None, hour: int = 12) -> datetime | None:
    """レポートは日付だけなので、その日の昼（日本時間）として扱う。"""
    return jst_to_utc_naive(datetime.combine(d, time(hour, 0))) if d else None


def _country(s: str) -> str:
    s = (s or "").strip()
    if re.fullmatch(r"[A-Za-z]{2}", s):
        return s.upper()
    return COUNTRIES.get(s.lower(), "")


@dataclass
class ReportLine:
    row_no: int
    order_number: str
    item_number: str
    sku: str
    title: str
    buyer: str
    quantity: int
    sold_for: Decimal | None
    shipping: Decimal | None
    currency: str
    ship_country: str
    ship_country_raw: str
    sale_date: date | None
    paid_date: date | None
    ship_by: date | None
    multi: bool = False  # まとめ買い（1注文に複数商品）

    @property
    def key(self) -> str:
        """ツール内の注文番号。まとめ買いは商品ごとに分けて記録するので商品番号を付ける。"""
        return f"{self.order_number}/{self.item_number or self.sku}" if self.multi else self.order_number

    @property
    def label(self) -> str:
        return f"{self.order_number} {self.sku or self.item_number}".strip()


def parse_report(data: bytes) -> list[ReportLine]:
    text = data.decode("utf-8-sig", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))
    header_idx, cols = None, {}
    for i, row in enumerate(rows[:30]):
        normed = [_norm(c) for c in row]
        found: dict[str, int] = {}
        for key, names in COLUMNS.items():
            for n in names:
                if n in normed:
                    found[key] = normed.index(n)
                    break
        if "order_number" in found and ("item_number" in found or "sku" in found) and "sold_for" in found:
            header_idx, cols = i, found
            break
    if header_idx is None:
        raise ServiceError("注文レポートの列（Order Number / Item Number / Sold For）が見つかりません。"
                           "Seller Hub の Reports → Downloads から「Orders」のレポートを選んでください")
    lines: list[ReportLine] = []
    for n, row in enumerate(rows[header_idx + 1:], start=header_idx + 2):
        def get(k: str) -> str:
            i = cols.get(k)
            return row[i].strip() if i is not None and i < len(row) else ""
        order_number = get("order_number")
        item_number, sku = get("item_number"), get("sku")
        if not order_number or not (item_number or sku):
            continue  # 空行・まとめ買いの合計行・末尾の「◯件ダウンロード」などの行
        try:
            qty = int(get("quantity") or 1)
        except ValueError:
            qty = 1
        raw_country = get("ship_country")
        lines.append(ReportLine(
            row_no=n, order_number=order_number, item_number=item_number, sku=sku, title=get("title"),
            buyer=get("buyer"), quantity=max(qty, 1), sold_for=_money(get("sold_for")),
            shipping=_money(get("shipping")), currency=_currency(get("sold_for")),
            ship_country=_country(raw_country), ship_country_raw=raw_country,
            sale_date=_date(get("sale_date")), paid_date=_date(get("paid_date")), ship_by=_date(get("ship_by"))))
    counts: dict[str, int] = {}
    for ln in lines:
        counts[ln.order_number] = counts.get(ln.order_number, 0) + 1
    for ln in lines:
        ln.multi = counts[ln.order_number] > 1
    return lines


@dataclass
class OrderImportResult:
    created: list[tuple[str, int]] = field(default_factory=list)   # (label, order id)
    paid: list[tuple[str, int]] = field(default_factory=list)
    linked: list[tuple[str, int]] = field(default_factory=list)    # 手で記録済みの注文に注文番号を付けた
    unchanged: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)   # (label, 理由)


def _find_listing(db: Session, ln: ReportLine) -> Listing | None:
    if ln.item_number:
        lst = db.scalar(select(Listing).where(Listing.ebay_item_id == ln.item_number).order_by(Listing.id.desc()))
        if lst:
            return lst
    if ln.sku:
        item = db.scalar(select(Item).where(Item.sku == ln.sku)) or \
            db.scalar(select(Item).where(Item.cert_number == ln.sku))
        if item:
            return db.scalar(select(Listing).where(Listing.item_id == item.id).order_by(Listing.id.desc()))
    return None


def _same_item(o: Order, ln: ReportLine) -> bool:
    if ln.item_number and o.listing is not None and o.listing.ebay_item_id:
        return o.listing.ebay_item_id == ln.item_number
    return not ln.sku or ln.sku in (o.item.sku, o.item.cert_number)


def _amount(ln: ReportLine, lst: Listing) -> Decimal | None:
    """ツールの記録ルールに合わせる：オークション＝落札価格、通常出品＝合計金額（送料込み）。"""
    if ln.sold_for is None:
        return None
    if lst.format == LF.AUCTION:
        return ln.sold_for
    # Sold For は1個あたりの価格
    return ln.sold_for * ln.quantity + (ln.shipping or Decimal(0))


def _apply_payment(db: Session, user: User, o: Order, ln: ReportLine) -> bool:
    if ln.paid_date is None or o.status != OS.AWAITING_PAYMENT:
        return False
    order_svc.record_payment(db, user, o, _when(ln.paid_date), None)
    if ln.ship_by:
        # eBay が示す発送期限を優先（日本時間のその日の終わり）
        o.ship_by = jst_to_utc_naive(datetime.combine(ln.ship_by, time(23, 59)))
    return True


def import_orders_report(db: Session, user: User, data: bytes) -> OrderImportResult:
    res = OrderImportResult()
    for ln in parse_report(data):
        try:
            _import_line(db, user, ln, res)
        except ServiceError as e:
            res.skipped.append((ln.label, str(e)))
    audit.log(db, audit.Actor.user(user), "order", None, "order.import_report",
              after={"created": [k for k, _ in res.created], "paid": [k for k, _ in res.paid],
                     "linked": [k for k, _ in res.linked], "skipped": res.skipped})
    if res.created or res.paid or res.linked:
        notify.enqueue_post(db, "orders", f"📥 注文レポートの取り込み：新規 {len(res.created)}件 / "
                                          f"支払記録 {len(res.paid)}件 / 照合 {len(res.linked)}件")
    return res


def _import_line(db: Session, user: User, ln: ReportLine, res: OrderImportResult) -> None:
    # レポートの絞り込み方でまとめ買いの行が欠けることがあるので、両方の書き方で探す
    keys = {ln.order_number, f"{ln.order_number}/{ln.item_number or ln.sku}"}
    o = next((x for x in db.scalars(select(Order).where(Order.ebay_order_id.in_(keys)))
              if _same_item(x, ln)), None)
    if o is not None:
        if _apply_payment(db, user, o, ln):
            res.paid.append((ln.label, o.id))
        else:
            res.unchanged.append(ln.label)
        return
    lst = _find_listing(db, ln)
    if lst is None:
        res.skipped.append((ln.label, "ツールの出品が見つかりません（出品結果の取り込み前、またはツール外の出品）"))
        return
    # 手で「落札を記録」した注文（注文番号なし）があれば、それに注文番号を付けて使う
    manual = db.scalar(select(Order).where(Order.listing_id == lst.id, Order.ebay_order_id.is_(None),
                                           Order.quantity == ln.quantity, Order.status != OS.CANCELLED)
                       .order_by(Order.id))
    if manual is not None:
        manual.ebay_order_id = ln.key
        manual.version += 1
        audit.log(db, audit.Actor.user(user), "order", manual.id, "order.linked_report",
                  after={"ebay_order_id": ln.key})
        res.linked.append((ln.label, manual.id))
        if _apply_payment(db, user, manual, ln):
            res.paid.append((ln.label, manual.id))
        return
    if lst.status != "active":
        res.skipped.append((ln.label, "出品が既に終了しています（落札の記録済み・取り下げ済みの可能性）"))
        return
    if not ln.ship_country:
        res.skipped.append((ln.label, f"配送先の国「{ln.ship_country_raw or '空欄'}」を判別できません。手で記録してください"))
        return
    o = order_svc.create_order_from_sale(
        db, user, lst, buyer=ln.buyer, amount=_amount(ln, lst), currency=ln.currency,
        ship_country=ln.ship_country, sold_at=_when(ln.sale_date), ebay_order_id=ln.key, source="report",
        quantity=ln.quantity)
    res.created.append((ln.label, o.id))
    if _apply_payment(db, user, o, ln):
        res.paid.append((ln.label, o.id))
