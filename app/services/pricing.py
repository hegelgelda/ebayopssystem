"""価格決定（F0-3）。参考値の収集、ツール案の算出、提案と承認。"""
from __future__ import annotations

import math
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import audit
from app.models import Item, ItemStatus as IS, Listing, MarketPrice, Order, OrderStatus, PriceQuote, User
from app.services import fx, states
from app.services.errors import PermissionDenied, ServiceError, check_version
from app.settings_store import get_setting, rule_version
from app.timeutil import utcnow

# (source, metric) → 表示名
REFERENCE_ROWS: list[tuple[str, str, str]] = [
    ("courtyard_buyback", "median", "Courtyard 買取中央値"),
    ("courtyard_p2p", "median", "Courtyard P2P中央値"),
    ("pokeca", "psa10_price", "pokeca-chart PSA10現在値"),
    ("pokeca", "psa10_population", "pokeca-chart PSA10 population"),
    ("pokeca", "change_30d", "pokeca-chart 30日変化率(%)"),
    ("snkrdunk", "last_price", "スニダン PSA10直近約定"),
    ("pricecharting", "psa10", "PriceCharting PSA10週次"),
]


def _dec(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v).replace(",", ""))
    except Exception:  # noqa: BLE001
        raise ServiceError(f"金額が不正です: {v}")


def _latest(db: Session, model_id: str, source: str, metric: str) -> MarketPrice | None:
    return db.scalar(select(MarketPrice).where(
        MarketPrice.model_id == model_id, MarketPrice.source == source, MarketPrice.metric == metric)
        .order_by(MarketPrice.as_of.desc()).limit(1))


def breakeven_usd(db: Session, purchase_jpy: int | None, rate: Decimal) -> Decimal | None:
    """損益分岐価格（USD）：手数料・送料・関税（DDP）込みで仕入値を回収できる最低価格。"""
    if purchase_jpy is None:
        return None
    fees = get_setting(db, "fees")
    rate_f = float(rate)
    var = float(fees["ebay_final_value_rate"]) + float(fees["international_fee_rate"]) + float(fees["duty_rate"])
    if var >= 1:
        return None
    cost_usd = (purchase_jpy + float(fees["shipping_cost_jpy"])) / rate_f + float(fees["per_order_fee_usd"])
    return Decimal(math.ceil(cost_usd / (1 - var)))


def reference_values(db: Session, item: Item) -> dict[str, Any]:
    rate, rate_src = fx.usdjpy_on(db)
    rows = []
    values: dict[str, Any] = {}
    for source, metric, label in REFERENCE_ROWS:
        mp = _latest(db, item.model_id, source, metric)
        row: dict[str, Any] = {"key": f"{source}.{metric}", "label": label, "value": None, "currency": None,
                               "usd": None, "jpy": None, "count": None, "as_of": None}
        if mp is not None and mp.value is not None:
            v = Decimal(mp.value)
            row.update(value=str(v), currency=mp.currency, count=mp.count, as_of=mp.as_of.isoformat())
            if mp.currency == "USD":
                row["usd"] = str(v.quantize(Decimal("0.01")))
                row["jpy"] = str((v * rate).quantize(Decimal("1")))
            elif mp.currency == "JPY":
                row["jpy"] = str(v.quantize(Decimal("1")))
                row["usd"] = str((v / rate).quantize(Decimal("0.01")))
            values[f"{source}.{metric}"] = row
        rows.append(row)

    # 自社実績
    sold = db.execute(select(Order.total_amount, Order.currency, Order.sold_at).join(Item, Item.id == Order.item_id)
                      .where(Item.model_id == item.model_id, Order.status != OrderStatus.CANCELLED)
                      .order_by(Order.sold_at.desc()).limit(10)).all()
    unsold = db.scalar(select(func.count()).select_from(Listing).join(Item, Item.id == Listing.item_id)
                       .where(Item.model_id == item.model_id, Listing.status == "unsold")) or 0
    history = {"sold": [{"amount": str(a) if a is not None else None, "currency": c, "date": d.date().isoformat()}
                        for a, c, d in sold], "unsold_count": unsold}

    be = breakeven_usd(db, item.purchase_price_jpy, rate)
    prev = db.scalar(select(PriceQuote).where(PriceQuote.item_id == item.id, PriceQuote.approved_at.is_not(None))
                     .order_by(PriceQuote.approved_at.desc()).limit(1))
    return {
        "rows": rows, "values": values, "history": history, "fx": {"usdjpy": str(rate), "source": rate_src},
        "purchase_price_jpy": item.purchase_price_jpy, "breakeven_usd": str(be) if be is not None else None,
        "fees": get_setting(db, "fees"), "relist_count": item.relist_count,
        "previous": ({"start": str(prev.approved_start) if prev.approved_start is not None else None,
                      "bin": str(prev.approved_bin) if prev.approved_bin is not None else None,
                      "approved_at": prev.approved_at.isoformat()} if prev else None),
        "taken_at": utcnow().isoformat(),
    }


def _round(v: Decimal | None, step: int) -> Decimal | None:
    if v is None:
        return None
    step = max(1, int(step or 1))
    return (v / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * step


def suggest(db: Session, item: Item, refs: dict[str, Any]) -> tuple[Decimal | None, Decimal | None, str]:
    """(開始価格, 即決価格, 説明)。初期ルール：開始＝損益分岐、即決＝海外水準×係数。再出品は前回 -5%。"""
    rule = get_setting(db, "price_rule")
    step = rule.get("round_to", 1)
    prev = refs.get("previous")
    if item.relist_count and prev and prev.get("start"):
        disc = Decimal(str(1 - float(rule.get("relist_discount", 0.05))))
        start = _round(Decimal(prev["start"]) * disc, step)
        bin_ = _round(Decimal(prev["bin"]) * disc, step) if prev.get("bin") else None
        be = _dec(refs.get("breakeven_usd"))
        note = f"再出品{item.relist_count}回目：前回価格の{int(float(rule.get('relist_discount', 0.05)) * 100)}%引き"
        if be is not None and start is not None and start < be:
            note += f"（損益分岐 ${be} を下回ります）"
        return start, bin_, note
    start = _dec(refs.get("breakeven_usd")) if rule.get("start", "breakeven") == "breakeven" else None
    base = None
    base_label = ""
    for src in rule.get("bin_sources", []):
        key = {"pricecharting_psa10": "pricecharting.psa10", "courtyard_p2p": "courtyard_p2p.median",
               "courtyard_buyback": "courtyard_buyback.median"}.get(src, src)
        row = refs["values"].get(key)
        if row and row.get("usd"):
            base = Decimal(row["usd"])
            base_label = row["label"]
            break
    bin_ = None
    note = "開始価格＝損益分岐価格"
    if base is not None:
        coef = Decimal(str(rule.get("bin_coefficient", 1.2)))
        bin_ = _round(base * coef, step)
        note += f"、即決価格＝{base_label} ${base} × {coef}"
        if start is not None and bin_ is not None and bin_ <= start:
            note += "（即決案が開始価格以下のため即決なし）"
            bin_ = None
    else:
        note += "、海外水準の参考値がないため即決案なし"
    return _round(start, step), bin_, note


def open_quote(db: Session, item: Item) -> PriceQuote | None:
    return db.scalar(select(PriceQuote).where(PriceQuote.item_id == item.id, PriceQuote.approved_at.is_(None))
                     .order_by(PriceQuote.id.desc()).limit(1))


def latest_approved(db: Session, item: Item) -> PriceQuote | None:
    return db.scalar(select(PriceQuote).where(PriceQuote.item_id == item.id, PriceQuote.approved_at.is_not(None))
                     .order_by(PriceQuote.approved_at.desc(), PriceQuote.id.desc()).limit(1))


def ensure_open_quote(db: Session, item: Item) -> PriceQuote:
    q = open_quote(db, item)
    refs = reference_values(db, item)
    start, bin_, note = suggest(db, item, refs)
    if q is None:
        q = PriceQuote(item_id=item.id, format=get_setting(db, "price_rule").get("default_format", "auction"))
        db.add(q)
    q.snapshot_json = {**refs, "rule_note": note}
    q.suggested_start, q.suggested_bin = start, bin_
    q.rule_version = rule_version(db)
    db.flush()
    return q


def pending_items(db: Session) -> list[Item]:
    """承認待ち：撮影済み、または再出品で新しい価格案が出ている価格確定の在庫。"""
    sub = select(PriceQuote.item_id).where(PriceQuote.approved_at.is_(None))
    return list(db.scalars(select(Item).where(
        (Item.status == IS.PHOTOGRAPHED) | ((Item.status == IS.PRICED) & Item.id.in_(sub)))
        .order_by(Item.status_changed_at)).all())


def propose(db: Session, user: User, item: Item, start, bin_, fmt: str, note: str | None) -> PriceQuote:
    if item.status not in (IS.PHOTOGRAPHED, IS.PRICED):
        raise ServiceError("撮影済み・価格確定の在庫にのみ価格を提案できます")
    q = ensure_open_quote(db, item)
    q.proposed_start, q.proposed_bin = _dec(start), _dec(bin_)
    q.format = fmt if fmt in ("auction", "fixed") else "auction"
    q.proposed_by, q.proposed_at, q.proposal_note = user.id, utcnow(), note
    if q.proposed_start is None or q.proposed_start <= 0:
        raise ServiceError("開始価格を入力してください")
    audit.log(db, audit.Actor.user(user), "item", item.id, "price.propose",
              after={"start": q.proposed_start, "bin": q.proposed_bin, "format": q.format, "note": note})
    return q


def approve(db: Session, user: User, item: Item, start, bin_, fmt: str, version: int | None) -> PriceQuote:
    if user.level < 2:
        raise PermissionDenied("価格の承認はLv2のみ可能です")
    check_version(item, version)
    if item.status not in (IS.PHOTOGRAPHED, IS.PRICED):
        raise ServiceError("撮影済みの在庫（または再出品候補）のみ承認できます")
    q = ensure_open_quote(db, item)  # 承認時点の参考値をスナップショットとして保存
    q.approved_start, q.approved_bin = _dec(start), _dec(bin_)
    if q.approved_start is None or q.approved_start <= 0:
        raise ServiceError("開始価格を入力してください")
    fmt = fmt if fmt in ("auction", "fixed") else "auction"
    if fmt == "fixed":
        if q.approved_bin is None:
            q.approved_bin = q.approved_start
    elif q.approved_bin is not None and q.approved_bin <= q.approved_start:
        raise ServiceError("即決価格は開始価格より高くしてください")
    q.format = fmt
    q.approved_by, q.approved_at = user.id, utcnow()
    # 提案者なし（承認者が直接価格を入れた）も自己承認として扱う
    q.self_approved = q.proposed_by is None or q.proposed_by == user.id
    reason = "自己承認" if q.self_approved else None
    audit.log(db, audit.Actor.user(user), "item", item.id, "price.approve",
              after={"quote_id": q.id, "start": q.approved_start, "bin": q.approved_bin, "format": fmt,
                     "suggested_start": q.suggested_start, "suggested_bin": q.suggested_bin,
                     "proposed_start": q.proposed_start, "proposed_bin": q.proposed_bin,
                     "rule_version": q.rule_version}, reason=reason)
    if item.status == IS.PHOTOGRAPHED:
        states.transition_item(db, item, IS.PRICED, audit.Actor.user(user), action="item.priced")
    else:
        item.version += 1
    return q
