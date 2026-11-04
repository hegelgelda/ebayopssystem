"""在庫（item）の登録・更新・取り下げ（F0-1, 5.1）。"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import audit
from app.models import Item, ItemStatus as IS, Listing, User
from app.services import cardinfo, states
from app.services.errors import NotFound, PermissionDenied, ServiceError, bump, check_version
from app.settings_store import get_setting
from app.timeutil import utcnow

EDITABLE_FIELDS = ["model_id", "card_name_jp", "card_name_en", "set_id", "set_name", "card_number", "grader",
                   "grade", "psa_population", "year", "storage_box", "storage_slot", "memo"]
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


def register_item(db: Session, user: User, data: dict[str, Any]) -> Item:
    cert = cardinfo.normalize_cert(data.get("cert_number", ""))
    if not cert:
        raise ServiceError("cert番号を入力してください")
    existing = find_by_cert(db, cert)
    if existing:
        err = ServiceError(f"cert番号 {cert} は登録済みです（在庫ID {existing.id}）")
        err.existing_item_id = existing.id  # type: ignore[attr-defined]
        raise err
    d = _clean({k: data.get(k) for k in EDITABLE_FIELDS + LV2_FIELDS + ["info_source"]})
    if not d.get("model_id"):
        d["model_id"] = cardinfo.make_model_id(d.get("set_id"), d.get("card_number"))
    if not d.get("model_id"):
        raise ServiceError("モデルID（セットID-型番）は必須です。候補から選ぶか、セットIDと型番を入力してください")
    if "-" in d["model_id"] and not d.get("set_id"):
        d["set_id"], num = d["model_id"].split("-", 1)
        d["card_number"] = d.get("card_number") or num
    if d.get("purchase_price_jpy") is None:
        raise ServiceError("仕入値（円）は必須です")
    if not d.get("storage_box"):
        raise ServiceError("保管場所（ボックス番号）は必須です")
    d["grader"] = d.get("grader") or "PSA"
    d["purchase_date"] = d.get("purchase_date") or date.today()
    item = Item(cert_number=cert, registered_by=user.id, status=IS.INTAKE, **d)
    db.add(item)
    db.flush()
    audit.log(db, audit.Actor.user(user), "item", item.id, "item.create",
              after={"cert_number": cert, **{k: v for k, v in d.items()}})
    return item


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
        if k == "model_id" and not v:
            raise ServiceError("モデルIDは必須です")
        before[k], after[k] = old, v
        setattr(item, k, v)
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


def status_counts(db: Session) -> dict[str, int]:
    rows = db.execute(select(Item.status, func.count()).group_by(Item.status)).all()
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
