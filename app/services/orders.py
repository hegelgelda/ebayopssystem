"""注文（order）の記録・期限・発送・追跡（5.2, F2-4, F3）。アダプタに依存しない共通処理。"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit
from app.models import (
    ChecklistItem, Item, ItemStatus as IS, Listing, ListingFormat as LF, Order, OrderStatus as OS, Shipment,
    TrackingEvent, User,
)
from app.services import fx, notify, pricing, states
from app.services.errors import NotFound, PermissionDenied, ServiceError, check_version
from app.settings_store import get_setting
from app.timeutil import fmt_jst, ship_by_from_paid, utcnow

TRACKING_STATUSES = {
    "picked_up": "集荷済み", "in_transit": "輸送中", "customs": "通関中", "customs_hold": "通関保留",
    "exception": "配達例外", "delivered": "配達完了",
}
CARRIERS = {"FEDEX": "FedEx", "DHL": "DHL Express", "SPEEDPAK": "eBay SpeedPAK", "JPPOST": "日本郵便",
            "NONE": "追跡なし（エコノミー）"}
NO_TRACKING = "NO-TRACKING"
TRACKING_URLS = {
    "FEDEX": "https://www.fedex.com/fedextrack/?trknbr={n}",
    "DHL": "https://www.dhl.com/global-en/home/tracking/tracking-express.html?submit=1&tracking-id={n}",
    "JPPOST": "https://trackings.post.japanpost.jp/services/srv/search/direct?reqCodeNo1={n}&locale=en",
}


def get_order(db: Session, order_id: int) -> Order:
    o = db.get(Order, order_id)
    if not o:
        raise NotFound("注文が見つかりません")
    return o


def order_label(o: Order) -> str:
    it = o.item
    qty = f" ×{o.quantity}" if (o.quantity or 1) > 1 else ""
    return f"#{o.id} {it.display_name}{qty} ({it.key_label})"


def listing_remaining(db: Session, listing: Listing) -> int:
    """その eBay 出品で、まだ売れていない数量（取消した注文は数えない）。"""
    from sqlalchemy import func
    sold = db.scalar(select(func.coalesce(func.sum(Order.quantity), 0)).where(
        Order.listing_id == listing.id, Order.status != OS.CANCELLED)) or 0
    return max(0, (listing.quantity or 1) - int(sold))


def create_order_from_sale(db: Session, user: User, listing: Listing, *, buyer: str, amount: Decimal,
                           currency: str, ship_country: str, sold_at: datetime | None, ebay_order_id: str | None,
                           source: str, quantity: int = 1) -> Order:
    if listing.status != "active":
        raise ServiceError("この出品は終了しています")
    item = db.get(Item, listing.item_id)
    assert item is not None
    if item.status != IS.LISTED:
        raise ServiceError("在庫が出品中ではありません")
    try:
        quantity = int(quantity or 1)
    except (TypeError, ValueError):
        raise ServiceError("数量は整数で入力してください")
    if quantity < 1 or quantity > item.remaining:
        raise ServiceError(f"数量は1〜{item.remaining}で入力してください（残り {item.remaining}）")
    left = listing_remaining(db, listing)
    if quantity > left:
        raise ServiceError(f"この出品の残りは {left} 点です（出品した数量 {listing.quantity}）")
    if listing.format == LF.AUCTION and quantity != 1:
        raise ServiceError("オークションの数量は1です")
    if amount is None or amount <= 0:
        raise ServiceError("落札価格を入力してください")
    ship_country = (ship_country or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", ship_country):
        raise ServiceError("配送先国は2文字の国コード（例：US, GB, DE）で入力してください")
    sold_at = sold_at or utcnow()
    days = int(get_setting(db, "payment_due_days") or 4)
    o = Order(ebay_order_id=ebay_order_id or None, listing_id=listing.id, item_id=item.id,
              buyer_username=(buyer or "").strip() or None, ship_country=ship_country, total_amount=amount,
              currency=currency or "USD", status=OS.AWAITING_PAYMENT, sold_at=sold_at,
              payment_due=sold_at + timedelta(days=days), source=source, quantity=quantity)
    db.add(o)
    listing.current_price = amount
    item.quantity_sold = (item.quantity_sold or 0) + quantity
    db.flush()
    if item.remaining == 0:
        # 売り切れ：出品も在庫も終わり
        listing.status = "sold"
        states.transition_item(db, item, IS.SOLD, audit.Actor.user(user), action="item.sold")
    elif listing_remaining(db, listing) == 0:
        # 出品した数量は売り切れたが、在庫はまだ残っている：eBay の出品は終わるので再出品へ
        listing.status = "sold"
        states.transition_item(db, item, IS.PRICED, audit.Actor.user(user), action="item.listing_sold_out",
                               reason=f"出品分が売り切れ（在庫の残り {item.remaining}）")
        notify.enqueue_post(db, "listing", f"🔁 出品した数量が売り切れました。在庫が {item.remaining} 点残っています："
                                           f"{item.key_label} {item.display_name}。出品準備から再出品してください")
    else:
        # 通常出品で在庫が残っている：出品はそのまま続く
        item.version += 1
        audit.log(db, audit.Actor.user(user), "item", item.id, "item.partially_sold",
                  after={"quantity_sold": item.quantity_sold, "remaining": item.remaining})
    audit.log(db, audit.Actor.user(user), "order", o.id, "order.create",
              after={"listing_id": listing.id, "ebay_item_id": listing.ebay_item_id, "buyer": o.buyer_username,
                     "quantity": quantity, "amount": amount, "currency": o.currency, "ship_country": ship_country, "source": source})
    notify.resolve(db, "listing", listing.id, ["auction_unrecorded"])
    notify.notify(db, "order_sold", "order", o.id,
                  f"🎉 落札・購入：{order_label(o)} / {o.currency} {amount} / 配送先 {ship_country}"
                  f"\n支払期限：{fmt_jst(o.payment_due)} JST", link_path=f"/orders/{o.id}")
    return o


def record_payment(db: Session, user: User, o: Order, paid_at: datetime | None, version: int | None) -> None:
    check_version(o, version)
    if o.status != OS.AWAITING_PAYMENT:
        raise ServiceError("支払待ちの注文ではありません")
    o.paid_at = paid_at or utcnow()
    o.ship_by = ship_by_from_paid(o.paid_at, int(get_setting(db, "ship_business_days") or 2))
    states.transition_order(db, o, OS.AWAITING_SHIPMENT, audit.Actor.user(user), action="order.paid")
    audit.log(db, audit.Actor.user(user), "order", o.id, "order.payment_recorded",
              after={"paid_at": o.paid_at, "ship_by": o.ship_by})
    notify.resolve(db, "order", o.id, ["payment_reminder", "payment_overdue"])
    notify.notify(db, "payment_completed", "order", o.id,
                  f"💳 支払完了：{order_label(o)}\n発送期限：{fmt_jst(o.ship_by)} JST", escalate_minutes=None,
                  link_path=f"/orders/{o.id}")


def cancel_unpaid(db: Session, user: User, o: Order, reason: str, version: int | None) -> None:
    if user.level < 2:
        raise PermissionDenied("注文の取消はLv2のみ可能です")
    check_version(o, version)
    if o.status != OS.AWAITING_PAYMENT:
        raise ServiceError("未払いの注文のみキャンセルできます")
    if not reason:
        raise ServiceError("理由の入力が必須です")
    states.transition_order(db, o, OS.CANCELLED, audit.Actor.user(user), reason=reason, action="order.cancel")
    o.cancelled_at = utcnow()
    item = o.item
    item.quantity_sold = max(0, (item.quantity_sold or 0) - (o.quantity or 1))
    if item.status == IS.SOLD:
        # 売り切れで終わっていた出品は戻らないので、価格確定に戻して再出品する
        states.transition_item(db, item, IS.PRICED, audit.Actor.user(user), reason="未払いキャンセル",
                               action="item.back_to_priced")
    else:
        item.version += 1
        audit.log(db, audit.Actor.user(user), "item", item.id, "item.quantity_returned",
                  after={"quantity_sold": item.quantity_sold, "remaining": item.remaining},
                  reason="未払いキャンセル（eBay側の在庫数も戻してください）")
    notify.resolve(db, "order", o.id, ["order_sold", "payment_reminder", "payment_overdue"])
    notify.enqueue_post(db, "orders", f"❌ 未払いキャンセル：{order_label(o)}（理由：{reason}）", order_id=o.id)


def record_unsold(db: Session, user: User, listing: Listing) -> None:
    """オークションの不落札／通常出品の出品終了（売れ残り）→ 在庫は PRICED に戻り、値下げ案を出す。"""
    if listing.status != "active":
        raise ServiceError("この出品は終了しています")
    item = db.get(Item, listing.item_id)
    assert item is not None
    listing.status = "unsold"
    item.relist_count += 1
    states.transition_item(db, item, IS.PRICED, audit.Actor.user(user), action="item.unsold")
    audit.log(db, audit.Actor.user(user), "listing", listing.id, "listing.unsold",
              after={"relist_count": item.relist_count})
    notify.resolve(db, "listing", listing.id, ["auction_unrecorded"])
    pricing.ensure_open_quote(db, item)


def validate_tracking(db: Session, carrier: str, number: str) -> str:
    carrier = (carrier or "").upper()
    if carrier not in CARRIERS:
        raise ServiceError("配送方法を選んでください")
    if carrier == "NONE":
        return NO_TRACKING
    number = re.sub(r"[\s-]", "", number or "").upper()
    if not number:
        raise ServiceError("追跡番号を入力してください（追跡のない発送は「追跡なし（エコノミー）」を選びます）")
    rx = (get_setting(db, "tracking_regex") or {}).get(carrier)
    if rx and not re.fullmatch(rx, number):
        raise ServiceError(f"{CARRIERS[carrier]} の追跡番号の形式ではありません")
    return number


def tracking_url(carrier: str, number: str) -> str:
    return TRACKING_URLS.get(carrier, "").format(n=number)


def add_shipment(db: Session, user: User, o: Order, carrier: str, number: str, version: int | None) -> Shipment:
    check_version(o, version)
    number = validate_tracking(db, carrier, number)
    carrier = carrier.upper()
    existing = o.active_shipment()
    if existing is not None:
        if user.level < 2:
            raise PermissionDenied("追跡番号の修正はLv2のみ可能です")
        existing.deleted = True
        audit.log(db, audit.Actor.user(user), "order", o.id, "shipment.replace",
                  before={"carrier": existing.carrier, "tracking_number": existing.tracking_number},
                  after={"carrier": carrier, "tracking_number": number})
    elif o.status != OS.AWAITING_SHIPMENT:
        raise ServiceError("発送待ちの注文にのみ追跡番号を登録できます")
    s = Shipment(order_id=o.id, carrier=carrier, tracking_number=number, entered_by=user.id)
    db.add(s)
    db.flush()
    db.refresh(o)
    if existing is None:
        o.shipped_at = utcnow()
        states.transition_order(db, o, OS.SHIPPED, audit.Actor.user(user), action="order.shipped")
        audit.log(db, audit.Actor.user(user), "order", o.id, "shipment.create",
                  after={"carrier": carrier, "tracking_number": number})
        notify.resolve(db, "order", o.id, ["ship_due_today", "ship_due_escalation", "ship_overdue"])
    else:
        o.version += 1
    return s


def delete_shipment(db: Session, user: User, o: Order, reason: str, version: int | None) -> None:
    if user.level < 2:
        raise PermissionDenied("追跡番号の削除はLv2のみ可能です")
    check_version(o, version)
    s = o.active_shipment()
    if not s:
        raise ServiceError("追跡番号が登録されていません")
    if not reason:
        raise ServiceError("理由の入力が必須です")
    s.deleted = True
    audit.log(db, audit.Actor.user(user), "order", o.id, "shipment.delete",
              before={"carrier": s.carrier, "tracking_number": s.tracking_number}, reason=reason)
    if o.status == OS.SHIPPED:
        states.transition_order(db, o, OS.AWAITING_SHIPMENT, audit.Actor.user(user), reason=reason,
                                manual=True, actor_user=user)
        o.shipped_at = None
    else:
        o.version += 1


def mark_ebay_uploaded(db: Session, user: User, s: Shipment) -> None:
    if s.ebay_upload_status == "uploaded":
        return
    s.ebay_upload_status = "uploaded"
    s.ebay_uploaded_at = utcnow()
    audit.log(db, audit.Actor.user(user), "order", s.order_id, "shipment.ebay_uploaded",
              after={"tracking_number": s.tracking_number, "method": "manual"})
    notify.resolve(db, "order", s.order_id, ["tracking_not_uploaded", "tracking_upload_failed"])
    notify.enqueue_post(db, "orders", f"🚚 追跡番号をeBayに登録しました：{CARRIERS.get(s.carrier)} {s.tracking_number}",
                        order_id=s.order_id)


def add_tracking_event(db: Session, user: User, o: Order, status: str, note: str | None) -> TrackingEvent:
    """F2-4：キャリアAPIが未設定の間は担当者が状態を選ぶ。"""
    if status not in TRACKING_STATUSES:
        raise ServiceError("配送状態が不正です")
    s = o.active_shipment()
    if not s:
        raise ServiceError("追跡番号が登録されていません")
    if o.status not in (OS.SHIPPED, OS.EXCEPTION, OS.DELIVERED):
        raise ServiceError("発送済みの注文ではありません")
    ev = TrackingEvent(shipment_id=s.id, normalized_status=status, raw_status=note or TRACKING_STATUSES[status],
                       occurred_at=utcnow(), entered_by=user.id)
    db.add(ev)
    db.flush()
    audit.log(db, audit.Actor.user(user), "order", o.id, "tracking.manual_event",
              after={"status": status, "note": note})
    actor = audit.Actor.user(user)
    if status in ("customs_hold", "exception"):
        if o.status == OS.SHIPPED:
            states.transition_order(db, o, OS.EXCEPTION, actor, action="order.exception")
        notify.notify(db, "transit_exception", "order", o.id,
                      f"⚠️ 輸送異常（{TRACKING_STATUSES[status]}）：{order_label(o)}{' / ' + note if note else ''}",
                      dedupe_key=str(ev.id), link_path=f"/orders/{o.id}")
    elif status == "delivered":
        if o.status in (OS.SHIPPED, OS.EXCEPTION):
            states.transition_order(db, o, OS.DELIVERED, actor, action="order.delivered")
            o.delivered_at = utcnow()
            notify.resolve(db, "order", o.id, ["transit_exception"])
            notify.enqueue_post(db, "orders", f"📦 配達完了：{order_label(o)}（48時間後に完了になります）", order_id=o.id)
    else:
        if o.status == OS.EXCEPTION:
            states.transition_order(db, o, OS.SHIPPED, actor, action="order.exception_resolved")
            notify.resolve(db, "order", o.id, ["transit_exception"])
    return ev


def open_case(db: Session, user: User, o: Order, note: str, version: int | None) -> None:
    check_version(o, version)
    states.transition_order(db, o, OS.CASE_OPEN, audit.Actor.user(user), reason=note, action="order.case_open")
    notify.notify(db, "case_opened", "order", o.id, f"🚨 ケース・返品リクエスト：{order_label(o)} / {note}",
                  mention="duty+lv2", dedupe_key=str(o.version), link_path=f"/orders/{o.id}")


def close_case(db: Session, user: User, o: Order, to: str, note: str, version: int | None) -> None:
    if user.level < 2:
        raise PermissionDenied("ケースの解決記録はLv2のみ可能です")
    check_version(o, version)
    if o.status != OS.CASE_OPEN:
        raise ServiceError("ケース対応中の注文ではありません")
    target = OS.COMPLETED if to == "COMPLETED" else (o.status_before_case or OS.SHIPPED)
    states.transition_order(db, o, target, audit.Actor.user(user), reason=note, action="order.case_closed")
    notify.resolve(db, "order", o.id, ["case_opened"])
    if target == OS.COMPLETED:
        finalize(db, o)


def set_status_manual(db: Session, user: User, o: Order, to: str, reason: str, version: int | None) -> None:
    check_version(o, version)
    if to not in OS.ALL:
        raise ServiceError("状態が不正です")
    states.transition_order(db, o, to, audit.Actor.user(user), reason=reason, manual=True, actor_user=user)
    if to == OS.COMPLETED:
        finalize(db, o)


def toggle_checklist(db: Session, user: User, o: Order, key: str, checked: bool) -> None:
    keys = [c["key"] for c in get_setting(db, "checklist_items")]
    if key not in keys:
        raise ServiceError("チェック項目が不正です")
    row = db.scalar(select(ChecklistItem).where(ChecklistItem.order_id == o.id, ChecklistItem.item_key == key))
    if checked and row is None:
        db.add(ChecklistItem(order_id=o.id, item_key=key, checked_by=user.id))
    elif not checked and row is not None:
        db.delete(row)
    else:
        return
    audit.log(db, audit.Actor.user(user), "order", o.id, "checklist.check" if checked else "checklist.uncheck",
              after={"key": key})


# ---- 発送サポート ----

def verify_pick(db: Session, user: User, o: Order, code: str) -> None:
    """取り出した在庫のラベル（SKU の QR／PSA のバーコード）を読み取り、注文の在庫と同じか確かめる。"""
    from app.services import items as item_svc
    code = (code or "").strip()
    if not code:
        raise ServiceError("ラベルを読み取るか、SKU・cert番号を入力してください")
    found = item_svc.find_by_key(db, code)
    if found is None:
        raise ServiceError(f"{code} の在庫が見つかりません")
    if found.id != o.item_id:
        audit.log(db, audit.Actor.user(user), "order", o.id, "order.pick_mismatch",
                  after={"scanned": found.key_label, "expected": o.item.key_label})
        raise ServiceError(f"⚠️ 別の在庫です：読み取ったのは {found.key_label} {found.display_name}。"
                           f"この注文は {o.item.key_label} {o.item.display_name} です")
    audit.log(db, audit.Actor.user(user), "order", o.id, "order.pick_verified", after={"code": code})
    if db.scalar(select(ChecklistItem).where(ChecklistItem.order_id == o.id,
                                             ChecklistItem.item_key == "picked")) is None:
        keys = [c["key"] for c in get_setting(db, "checklist_items")]
        if "picked" in keys:
            db.add(ChecklistItem(order_id=o.id, item_key="picked", checked_by=user.id))


def set_shipping_cost(db: Session, user: User, o: Order, cost: int | None) -> None:
    if cost is not None and (cost < 0 or cost > 1_000_000):
        raise ServiceError("送料は0〜1,000,000円で入力してください")
    if cost == o.shipping_cost_jpy:
        return
    audit.log(db, audit.Actor.user(user), "order", o.id, "order.shipping_cost",
              before={"shipping_cost_jpy": o.shipping_cost_jpy}, after={"shipping_cost_jpy": cost})
    o.shipping_cost_jpy = cost
    o.version += 1
    if o.status == OS.COMPLETED:
        finalize(db, o)  # 確定済みの粗利も入れ直す


def shipping_queue(db: Session, now: datetime | None = None) -> dict[str, list[Order]]:
    """発送ページ：期限切れ／今日／明日以降の発送待ち、eBayに追跡番号が未登録の発送済み。"""
    from app.timeutil import to_jst
    now = now or utcnow()
    today = to_jst(now).date()  # type: ignore[union-attr]
    waiting = db.scalars(select(Order).where(Order.status == OS.AWAITING_SHIPMENT)).all()
    out: dict[str, list[Order]] = {"overdue": [], "today": [], "later": [], "unpaid": [], "not_uploaded": []}
    for o in sorted(waiting, key=lambda x: x.ship_by or datetime.max):
        if o.ship_by and o.ship_by <= now:
            out["overdue"].append(o)
        elif o.ship_by and to_jst(o.ship_by).date() <= today:  # type: ignore[union-attr]
            out["today"].append(o)
        else:
            out["later"].append(o)
    out["unpaid"] = list(db.scalars(select(Order).where(Order.status == OS.AWAITING_PAYMENT)
                                    .order_by(Order.sold_at)).all())
    out["not_uploaded"] = list(db.scalars(
        select(Order).join(Shipment, Shipment.order_id == Order.id)
        .where(Order.status == OS.SHIPPED, Shipment.deleted.is_(False), Shipment.ebay_upload_status != "uploaded")
        .order_by(Order.id)).unique().all())
    return out


def pick_sort_key(o: Order) -> tuple:
    """ピッキングリストは保管場所の順（ボックス→スロット）。"""
    it = o.item
    return (it.storage_box or "~", _natural(it.storage_slot), it.sku or "")


def _natural(s: str | None) -> tuple:
    return tuple(int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s or ""))


def gross_profit(db: Session, o: Order) -> dict[str, Any] | None:
    """粗利（円）。為替は落札日の USD/JPY。"""
    if o.total_amount is None or o.item.purchase_price_jpy is None:  # 仕入値は1個あたり
        return None
    if o.currency == "JPY":
        rate, src = Decimal(1), "JPY"
    else:
        rate, src = fx.usdjpy_on(db, o.sold_at)
    fees = get_setting(db, "fees")
    amt = Decimal(o.total_amount)
    revenue = amt * rate
    ebay_fees = (amt * Decimal(str(fees["ebay_final_value_rate"] + fees["international_fee_rate"]))
                 + Decimal(str(fees["per_order_fee_usd"]))) * rate
    duty = amt * Decimal(str(fees["duty_rate"])) * rate
    actual_ship = o.shipping_cost_jpy is not None
    ship = Decimal(o.shipping_cost_jpy) if actual_ship else Decimal(str(fees["shipping_cost_jpy"]))
    purchase = o.item.purchase_price_jpy * (o.quantity or 1)
    gp = revenue - ebay_fees - duty - ship - Decimal(purchase)
    return {"rate": rate, "source": src, "revenue_jpy": int(revenue), "fees_jpy": int(ebay_fees),
            "duty_jpy": int(duty), "shipping_jpy": int(ship), "shipping_actual": actual_ship,
            "purchase_jpy": purchase,
            "gross_profit_jpy": int(gp)}


def finalize(db: Session, o: Order) -> None:
    o.completed_at = o.completed_at or utcnow()
    gp = gross_profit(db, o)
    if gp:
        o.fx_rate_jpy, o.fx_source, o.gross_profit_jpy = gp["rate"], gp["source"], gp["gross_profit_jpy"]
        audit.log(db, audit.Actor.system(), "order", o.id, "order.gross_profit", after=gp)


# ---- 期限・時間経過のチェック（worker が5分ごとに呼ぶ） ----

def run_deadline_checks(db: Session, now: datetime | None = None) -> None:
    from app.timeutil import to_jst
    now = now or utcnow()
    now_jst = to_jst(now)
    assert now_jst is not None

    # 支払期限の24時間前・未払い / 期限超過でLv2へ
    for o in db.scalars(select(Order).where(Order.status == OS.AWAITING_PAYMENT, Order.payment_due.is_not(None))):
        if now >= o.payment_due - timedelta(hours=24):
            notify.notify(db, "payment_reminder", "order", o.id,
                          f"⏰ 支払期限まで24時間を切りました（未払い）：{order_label(o)} 期限 {fmt_jst(o.payment_due)} JST",
                          escalate_at=o.payment_due, link_path=f"/orders/{o.id}")
        if now >= o.payment_due:
            notify.notify(db, "payment_overdue", "order", o.id,
                          f"⛔ 支払期限を超過しました：{order_label(o)}。未払いキャンセルの手続きを検討してください",
                          mention="duty+lv2", escalate_minutes=None, link_path=f"/orders/{o.id}")

    esc_hour = int(get_setting(db, "ship_due_escalate_hour_jst") or 12)
    notify_hour = int(get_setting(db, "ship_due_notify_hour_jst") or 9)
    for o in db.scalars(select(Order).where(Order.status == OS.AWAITING_SHIPMENT, Order.ship_by.is_not(None))):
        due_jst = to_jst(o.ship_by)
        assert due_jst is not None
        if now >= o.ship_by:
            notify.notify(db, "ship_overdue", "order", o.id, f"⛔ 発送期限を超過しました：{order_label(o)}",
                          mention="duty+lv2", escalate_minutes=None, link_path=f"/orders/{o.id}")
        elif due_jst.date() == now_jst.date():
            if now_jst.hour >= notify_hour:
                esc = due_jst.replace(hour=esc_hour, minute=0, second=0, microsecond=0)
                from app.timeutil import jst_to_utc_naive
                notify.notify(db, "ship_due_today", "order", o.id,
                              f"📮 本日が発送期限です：{order_label(o)}（{fmt_jst(o.ship_by)} JST まで）",
                              escalate_at=jst_to_utc_naive(esc), link_path=f"/orders/{o.id}")

    # 追跡番号を入れたが eBay 未登録
    for s in db.scalars(select(Shipment).where(Shipment.deleted.is_(False), Shipment.ebay_upload_status != "uploaded")):
        if now >= s.entered_at + timedelta(minutes=10):
            o = s.order
            notify.notify(db, "tracking_not_uploaded", "order", o.id,
                          f"📝 追跡番号がeBayに未登録です：{order_label(o)} {CARRIERS.get(s.carrier)} {s.tracking_number}",
                          dedupe_key=str(s.id), link_path=f"/orders/{o.id}")

    # オークション終了後に落札未記録（APIなし運用）
    for lst in db.scalars(select(Listing).where(Listing.status == "active", Listing.format == LF.AUCTION,
                                                Listing.end_time.is_not(None),
                                                Listing.end_time <= now)):
        notify.notify(db, "auction_unrecorded", "listing", lst.id,
                      f"🔔 出品の終了時刻を過ぎましたが、落札・不落札が未記録です：{lst.title}（eBay {lst.ebay_item_id}）",
                      channel="listing", link_path=f"/items/{lst.item_id}")

    # 配達完了から48時間 → COMPLETED
    hours = int(get_setting(db, "delivered_to_completed_hours") or 48)
    for o in db.scalars(select(Order).where(Order.status == OS.DELIVERED, Order.delivered_at.is_not(None))):
        if now >= o.delivered_at + timedelta(hours=hours):
            states.transition_order(db, o, OS.COMPLETED, audit.Actor.system(), action="order.completed")
            finalize(db, o)


def todays_orders(db: Session) -> list[Order]:
    """ダッシュボード：対応が必要な注文を期限の近い順に。"""
    rows = list(db.scalars(select(Order).where(Order.status.in_(
        [OS.AWAITING_PAYMENT, OS.AWAITING_SHIPMENT, OS.EXCEPTION, OS.CASE_OPEN]))).all())
    shipped_pending = db.scalars(select(Order).join(Shipment, Shipment.order_id == Order.id).where(
        Order.status == OS.SHIPPED, Shipment.deleted.is_(False), Shipment.ebay_upload_status != "uploaded")).all()
    for o in shipped_pending:
        if o not in rows:
            rows.append(o)

    def key(o: Order):
        d = o.ship_by if o.status == OS.AWAITING_SHIPMENT else (
            o.payment_due if o.status == OS.AWAITING_PAYMENT else o.updated_at)
        return d or datetime.max
    return sorted(rows, key=key)


def deadline_of(o: Order) -> tuple[str, datetime | None]:
    if o.status == OS.AWAITING_PAYMENT:
        return "支払期限", o.payment_due
    if o.status == OS.AWAITING_SHIPMENT:
        return "発送期限", o.ship_by
    if o.status == OS.DELIVERED and o.delivered_at:
        return "破損報告の窓口", o.delivered_at + timedelta(hours=48)
    return "", None

