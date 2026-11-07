"""在庫と注文の状態機械（5章）。遷移は必ずここを通し、操作ログに残す。"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app import audit
from app.models import Item, ItemStatus as IS, Order, OrderStatus as OS
from app.services.errors import PermissionDenied, ServiceError, bump
from app.timeutil import utcnow

ITEM_TRANSITIONS: dict[str, set[str]] = {
    IS.INTAKE: {IS.PHOTOGRAPHED},
    IS.PHOTOGRAPHED: {IS.PRICED},
    IS.PRICED: {IS.READY},
    IS.READY: {IS.LISTED, IS.PRICED},  # 下書きの再生成・出品形式の変更で価格確定に戻す
    IS.LISTED: {IS.PRICED, IS.SOLD},  # 不落札 → PRICED / 落札 → SOLD
    IS.SOLD: {IS.PRICED},  # 未払いキャンセルで在庫に戻る
    IS.WITHDRAWN: set(),
}

ORDER_TRANSITIONS: dict[str, set[str]] = {
    OS.AWAITING_PAYMENT: {OS.CANCELLED, OS.AWAITING_SHIPMENT},
    OS.AWAITING_SHIPMENT: {OS.SHIPPED},
    OS.SHIPPED: {OS.EXCEPTION, OS.DELIVERED},
    OS.EXCEPTION: {OS.SHIPPED, OS.DELIVERED},
    OS.DELIVERED: {OS.COMPLETED},
    OS.COMPLETED: set(),
    OS.CANCELLED: set(),
    OS.CASE_OPEN: set(),  # 解決は close_case で元の状態または COMPLETED
}


def _require_lv2(actor_user, what: str) -> None:
    if actor_user is None or actor_user.level < 2:
        raise PermissionDenied(f"{what}はLv2のみ可能です")


def transition_item(db: Session, item: Item, to: str, actor: audit.Actor, *, reason: str | None = None,
                    manual: bool = False, actor_user=None, action: str | None = None) -> None:
    """manual=True は Lv2 による任意の変更（巻き戻し・取り下げ）。理由必須。"""
    frm = item.status
    if frm == to:
        return
    if to == IS.WITHDRAWN or manual:
        _require_lv2(actor_user, "在庫の取り下げ・状態の手動変更")
        if not reason or not reason.strip():
            raise ServiceError("理由の入力が必須です")
        if to in (IS.LISTED, IS.SOLD):
            raise ServiceError("出品中・売却済みへの変更は出品・落札の記録から行ってください")
    elif to not in ITEM_TRANSITIONS.get(frm, set()):
        raise ServiceError(f"在庫の状態を {IS.LABELS.get(frm, frm)} から {IS.LABELS.get(to, to)} に変更できません")
    item.status = to
    item.status_changed_at = utcnow()
    if to == IS.WITHDRAWN:
        item.withdraw_reason = reason
    bump(item)
    audit.log(db, actor, "item", item.id, action or ("item.status_manual" if manual else "item.status"),
              before={"status": frm}, after={"status": to}, reason=reason)


def transition_order(db: Session, order: Order, to: str, actor: audit.Actor, *, reason: str | None = None,
                     manual: bool = False, actor_user=None, action: str | None = None) -> None:
    frm = order.status
    if frm == to:
        return
    if manual:
        _require_lv2(actor_user, "注文ステータスの変更")
        if not reason or not reason.strip():
            raise ServiceError("理由の入力が必須です")
    elif to == OS.CASE_OPEN:
        if frm in (OS.CANCELLED, OS.CASE_OPEN):
            raise ServiceError("この注文にはケースを記録できません")
        order.status_before_case = frm
    elif frm == OS.CASE_OPEN:
        if to not in (order.status_before_case, OS.COMPLETED):
            raise ServiceError("ケースの解決後は元の状態か完了にのみ戻せます")
    elif to not in ORDER_TRANSITIONS.get(frm, set()):
        raise ServiceError(f"注文の状態を {OS.LABELS.get(frm, frm)} から {OS.LABELS.get(to, to)} に変更できません")
    order.status = to
    bump(order)
    audit.log(db, actor, "order", order.id, action or ("order.status_manual" if manual else "order.status"),
              before={"status": frm}, after={"status": to}, reason=reason)
