"""在庫（item）の登録・更新・取り下げ（F0-1, 5.1）。"""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import audit
from app.models import Category, Item, ItemStatus as IS, Listing, ListingFormat as LF, User
from app.services import cardinfo, states
from app.services.errors import NotFound, PermissionDenied, ServiceError, bump, check_version
from app.settings_store import get_setting
from app.timeutil import utcnow

EDITABLE_FIELDS = ["model_id", "card_name_jp", "card_name_en", "set_id", "set_name", "card_number", "grader",
                   "grade", "psa_population", "year", "storage_box", "storage_slot", "memo", "condition"]
LV2_FIELDS = ["purchase_price_jpy", "purchase_date"]


def can_see_purchase_price(db: Session, user: User, item: Item | None = None) -> bool:
    if user.level >= 2:
        return True
    if item is not None and item.registered_by == user.id:
        return True
    return bool(get_setting(db, "show_purchase_price_to_lv1"))


def get_item(db: Session, item_id: int) -> Item:
    item = db.get(Item, item_id)
    if not item:
        raise NotFound("在庫が見つかりません")
    return item


def find_by_cert(db: Session, cert: str) -> Item | None:
    return db.scalar(select(Item).where(Item.cert_number == cert))


def find_by_key(db: Session, key: str) -> Item | None:
    """SKU（例 S-00012）または cert 番号で探す。"""
    key = (key or "").strip()
    if not key:
        return None
    it = db.scalar(select(Item).where(Item.sku == key.upper())) or db.scalar(select(Item).where(Item.sku == key))
    if it:
        return it
    cert = cardinfo.normalize_cert(key)
    return find_by_cert(db, cert) if cert else None


def _clean(data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in data.items():
        if isinstance(v, str):
            v = v.strip()
            if v == "":
                v = None
        out[k] = v
    if out.get("psa_population") is not None:
        try:
            out["psa_population"] = int(out["psa_population"])
        except ValueError:
            raise ServiceError("Populationは数値で入力してください")
    if out.get("purchase_price_jpy") is not None:
        try:
            out["purchase_price_jpy"] = int(str(out["purchase_price_jpy"]).replace(",", ""))
        except ValueError:
            raise ServiceError("仕入値は円の整数で入力してください")
        if out["purchase_price_jpy"] < 0:
            raise ServiceError("仕入値が不正です")
    if isinstance(out.get("purchase_date"), str):
        out["purchase_date"] = date.fromisoformat(out["purchase_date"])
    return out


def _int_field(v: Any, label: str, minimum: int = 0) -> int | None:
    if v in (None, ""):
        return None
    try:
        n = int(str(v).replace(",", ""))
    except ValueError:
        raise ServiceError(f"{label}は整数で入力してください")
    if n < minimum:
        raise ServiceError(f"{label}が不正です")
    return n


def default_format(db: Session, category: str) -> str:
    fmt = (get_setting(db, "default_format_by_category") or {}).get(category, LF.AUCTION)
    return fmt if fmt in LF.ALL else LF.AUCTION


def register_item(db: Session, user: User, data: dict[str, Any]) -> Item:
    """区分ごとに必須項目を確認して在庫を作る。PSA は cert 番号、それ以外は SKU を自動で振る。"""
    category = (data.get("category") or Category.PSA).strip()
    if category not in Category.ALL:
        raise ServiceError("区分が不正です")
    d = _clean({k: data.get(k) for k in EDITABLE_FIELDS + LV2_FIELDS + ["info_source", "condition"]})
    quantity = _int_field(data.get("quantity"), "数量", 1) or 1
    cert = None

    if category == Category.PSA:
        cert = cardinfo.normalize_cert(data.get("cert_number", ""))
        if not cert:
            raise ServiceError("cert番号を入力してください")
        existing = find_by_cert(db, cert)
        if existing:
            err = ServiceError(f"cert番号 {cert} は登録済みです（在庫ID {existing.id}）")
            err.existing_item_id = existing.id  # type: ignore[attr-defined]
            raise err
        quantity = 1
        d["grader"] = d.get("grader") or "PSA"
        if not d.get("grade"):
            raise ServiceError("グレードを選んでください")
        d["condition"] = None
    else:
        d["grader"] = None
        d["grade"] = None
        d["psa_population"] = None

    if category in (Category.PSA, Category.SINGLE):
        if not d.get("model_id"):
            d["model_id"] = cardinfo.make_model_id(d.get("set_id"), d.get("card_number"))
        if not d.get("model_id"):
            raise ServiceError("モデルID（セットID-型番）は必須です。型番で検索して候補から選んでください")
    if d.get("model_id") and "-" in d["model_id"] and not d.get("set_id"):
        d["set_id"], num = d["model_id"].split("-", 1)
        d["card_number"] = d.get("card_number") or num

    if category == Category.SINGLE:
        keys = [c["key"] for c in get_setting(db, "raw_conditions") or []]
        if d.get("condition") not in keys:
            raise ServiceError("カードの状態を選んでください")
    elif category != Category.PSA:
        d["condition"] = None
    if category in (Category.PACK, Category.BOX, Category.BULK):
        if not (d.get("card_name_jp") or d.get("card_name_en") or d.get("set_name")):
            raise ServiceError("商品名（またはセット名）を入力してください")
    if category in (Category.PACK, Category.BOX) and not (d.get("set_name") or d.get("set_id")):
        raise ServiceError("パック・BOX はセット名を入力してください（eBay の必須項目です）")
    if category == Category.BULK:
        quantity = 1  # バルクは1ロットで1出品

    fmt = data.get("listing_format") or default_format(db, category)
    if fmt not in LF.ALL:
        raise ServiceError("出品形式が不正です")
    if quantity > 1 and fmt == LF.AUCTION:
        raise ServiceError("数量が2以上の在庫はオークションにできません（通常出品を選んでください）")

    if d.get("purchase_price_jpy") is None:
        raise ServiceError("仕入値（円）は必須です")
    if not d.get("storage_box"):
        raise ServiceError("保管場所（ボックス番号）は必須です")
    d["purchase_date"] = d.get("purchase_date") or date.today()

    item = Item(category=category, sku=cert or f"tmp-{uuid.uuid4().hex[:24]}", cert_number=cert,
                listing_format=fmt, quantity=quantity, quantity_sold=0, registered_by=user.id,
                status=IS.INTAKE, **d)
    db.add(item)
    db.flush()
    if cert is None:
        item.sku = f"{Category.SKU_PREFIX[category]}-{item.id:05d}"
        db.flush()
    audit.log(db, audit.Actor.user(user), "item", item.id, "item.create",
              after={"category": category, "sku": item.sku, "cert_number": cert, "listing_format": fmt,
                     "quantity": quantity, **{k: v for k, v in d.items()}})
    return item


def set_listing_format(db: Session, user: User, item: Item, fmt: str, version: int | None) -> None:
    """レーン（通常出品／オークション）を切り替える。出品前のみ。"""
    check_version(item, version)
    if fmt not in LF.ALL:
        raise ServiceError("出品形式が不正です")
    if item.listing_format == fmt:
        return
    if item.status not in (IS.INTAKE, IS.PHOTOGRAPHED, IS.PRICED, IS.READY):
        raise ServiceError("出品中・売却済みの在庫は出品形式を変更できません")
    if fmt == LF.AUCTION and item.remaining > 1:
        raise ServiceError("数量が2以上の在庫はオークションにできません")
    before = item.listing_format
    item.listing_format = fmt
    bump(item)
    audit.log(db, audit.Actor.user(user), "item", item.id, "item.listing_format",
              before={"listing_format": before}, after={"listing_format": fmt})
    # 価格・下書きは形式ごとに作り直す（承認済み価格は残し、新しい形式で再承認する）
    from app.services import listing as listing_svc, pricing
    for dr in listing_svc.open_drafts(db, item):
        dr.status = "superseded"
    if item.status in (IS.PRICED, IS.READY):
        if item.status == IS.READY:
            states.transition_item(db, item, IS.PRICED, audit.Actor.user(user), action="item.format_changed")
        pricing.ensure_open_quote(db, item)
    elif item.status == IS.PHOTOGRAPHED:
        pricing.ensure_open_quote(db, item)


def update_item(db: Session, user: User, item: Item, data: dict[str, Any], version: int | None) -> None:
    check_version(item, version)
    d = _clean({k: data[k] for k in EDITABLE_FIELDS + LV2_FIELDS if k in data})
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    for k, v in d.items():
        old = getattr(item, k)
        if old == v:
            continue
        if k in LV2_FIELDS and user.level < 2:
            raise PermissionDenied("仕入値の修正はLv2のみ可能です")
        if k == "model_id" and not v and item.category in (Category.PSA, Category.SINGLE):
            raise ServiceError("モデルIDは必須です")
        before[k], after[k] = old, v
        setattr(item, k, v)
    qty = _int_field(data.get("quantity"), "数量", 1) if "quantity" in data else None
    if qty is not None and qty != item.quantity:
        if item.category in (Category.PSA, Category.BULK):
            raise ServiceError("PSA鑑定品・バルクの数量は1です")
        if qty < item.quantity_sold:
            raise ServiceError(f"数量は販売済みの {item.quantity_sold} 以上にしてください")
        if item.listing_format == LF.AUCTION and qty - item.quantity_sold > 1:
            raise ServiceError("オークションの在庫は数量1です。通常出品に切り替えてから変更してください")
        if item.status in (IS.LISTED, IS.SOLD):
            raise ServiceError("出品中・売却済みの数量は変更できません（eBay側と食い違うため）")
        before["quantity"], after["quantity"] = item.quantity, qty
        item.quantity = qty
    if not after:
        return
    bump(item)
    audit.log(db, audit.Actor.user(user), "item", item.id, "item.update", before=before, after=after)


def withdraw_item(db: Session, user: User, item: Item, reason: str, version: int | None) -> None:
    check_version(item, version)
    if item.status == IS.LISTED:
        lst = db.scalar(select(Listing).where(Listing.item_id == item.id, Listing.status == "active"))
        if lst:
            lst.status = "ended"
            audit.log(db, audit.Actor.user(user), "listing", lst.id, "listing.end", reason=reason,
                      after={"status": "ended"})
    states.transition_item(db, item, IS.WITHDRAWN, audit.Actor.user(user), reason=reason, actor_user=user)


def rollback_item(db: Session, user: User, item: Item, to: str, reason: str, version: int | None) -> None:
    check_version(item, version)
    if to not in (IS.INTAKE, IS.PHOTOGRAPHED, IS.PRICED, IS.READY):
        raise ServiceError("巻き戻し先が不正です")
    if item.status in (IS.LISTED, IS.SOLD):
        raise ServiceError("出品中・売却済みの在庫は、不落札・キャンセルの記録から戻してください")
    states.transition_item(db, item, to, audit.Actor.user(user), reason=reason, manual=True, actor_user=user)


def status_counts(db: Session, listing_format: str | None = None) -> dict[str, int]:
    q = select(Item.status, func.count()).group_by(Item.status)
    if listing_format:
        q = q.where(Item.listing_format == listing_format)
    rows = db.execute(q).all()
    out = {s: 0 for s in IS.ALL}
    out.update({s: c for s, c in rows})
    return out


def stale_items(db: Session) -> list[tuple[Item, int]]:
    """F3-4：INTAKE 7日・PHOTOGRAPHED 3日を超えた在庫。"""
    cfg = get_setting(db, "stale_days") or {}
    now = utcnow()
    out: list[tuple[Item, int]] = []
    for status, days in cfg.items():
        cutoff = now - timedelta(days=int(days))
        for it in db.scalars(select(Item).where(Item.status == status, Item.status_changed_at < cutoff)):
            out.append((it, (now - it.status_changed_at).days))
    out.sort(key=lambda x: -x[1])
    return out
