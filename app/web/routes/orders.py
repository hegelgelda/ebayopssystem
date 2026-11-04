"""注文一覧・注文詳細・追跡番号の入力（F6-7〜F6-10）。"""
from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app import audit
from app.adapters import get_order_adapter
from app.db import get_db
from app.models import (
    AuditLog, ChecklistItem, Comment, Item, Notification, Order, OrderStatus as OS, User,
)
from app.services import notify, orders as order_svc
from app.services.errors import ServiceError
from app.settings_store import get_setting
from app.web.deps import current_user, flash, render, require_lv2
from app.web.routes.common import form_dt_jst, form_int, to

router = APIRouter()


@router.get("/orders")
def orders_list(request: Request, status: str = "", assignee: str = "", date_from: str = "", date_to: str = "",
                q: str = "", user: User = Depends(current_user), db: Session = Depends(get_db)):
    stmt = select(Order).join(Item, Item.id == Order.item_id)
    if status == "open":
        stmt = stmt.where(Order.status.in_(OS.OPEN))
    elif status:
        stmt = stmt.where(Order.status == status)
    if assignee:
        stmt = stmt.where(Order.assignee_id == (None if assignee == "none" else int(assignee)))
    if date_from:
        stmt = stmt.where(Order.sold_at >= date.fromisoformat(date_from))
    if date_to:
        stmt = stmt.where(Order.sold_at < date.fromisoformat(date_to) + timedelta(days=1))
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Order.buyer_username.ilike(like), Order.ebay_order_id.like(like),
                              Item.cert_number.like(like), Item.card_name_en.ilike(like),
                              Item.card_name_jp.like(like)))
    rows = db.scalars(stmt.order_by(Order.id.desc()).limit(500)).all()
    users = db.scalars(select(User).where(User.is_active.is_(True))).all()
    return render(request, "orders_list.html", {
        "user": user, "orders": rows, "users": users,
        "f": {"status": status, "assignee": assignee, "date_from": date_from, "date_to": date_to, "q": q}})


@router.get("/orders/{order_id}")
def order_detail(order_id: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    o = order_svc.get_order(db, order_id)
    checks = {c.item_key: c for c in db.scalars(select(ChecklistItem).where(ChecklistItem.order_id == o.id))}
    comments = db.scalars(select(Comment).where(Comment.target_type == "order", Comment.target_id == o.id)
                          .order_by(Comment.id)).all()
    logs = db.scalars(select(AuditLog).where(AuditLog.target_type == "order", AuditLog.target_id == str(o.id))
                      .order_by(AuditLog.id.desc()).limit(200)).all()
    notes = db.scalars(select(Notification).where(Notification.target_type == "order",
                                                  Notification.target_id == o.id).order_by(Notification.id)).all()
    s = o.active_shipment()
    users = {u.id: u for u in db.scalars(select(User))}
    label, deadline = order_svc.deadline_of(o)
    return render(request, "order_detail.html", {
        "user": user, "o": o, "item": o.item, "checks": checks, "check_defs": get_setting(db, "checklist_items"),
        "comments": comments, "logs": logs, "notes": notes, "shipment": s, "users": users,
        "show_sensitive": user.level >= 2,
        "deadline_label": label, "deadline": deadline,
        "tracking_url": order_svc.tracking_url(s.carrier, s.tracking_number) if s else None,
        "tracking_statuses": order_svc.TRACKING_STATUSES, "carriers": order_svc.CARRIERS,
        "gp": order_svc.gross_profit(db, o) if user.level >= 2 else None, "OS": OS,
        "instructions": get_order_adapter(db).upload_tracking(db, user, s).instructions
        if s and s.ebay_upload_status != "uploaded" else []})


def _o(db: Session, order_id: int) -> Order:
    return order_svc.get_order(db, order_id)


@router.post("/orders/{order_id}/payment")
def order_payment(order_id: int, request: Request, paid_at: str = Form(""), version: str = Form(""),
                  user: User = Depends(current_user), db: Session = Depends(get_db)):
    o = _o(db, order_id)
    get_order_adapter(db).record_payment(db, user, o, form_dt_jst(paid_at), form_int(version))
    db.commit()
    flash(request, "支払を記録しました")
    return to(f"/orders/{o.id}")


@router.post("/orders/{order_id}/cancel")
def order_cancel(order_id: int, request: Request, reason: str = Form(""), version: str = Form(""),
                 user: User = Depends(require_lv2), db: Session = Depends(get_db)):
    o = _o(db, order_id)
    order_svc.cancel_unpaid(db, user, o, reason, form_int(version))
    db.commit()
    flash(request, "未払いキャンセルを記録しました。在庫は価格確定に戻りました")
    return to(f"/orders/{o.id}")


@router.post("/orders/{order_id}/claim")
def order_claim(order_id: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    o = _o(db, order_id)
    notify.claim_order(db, o, user, audit.Actor.user(user))
    db.commit()
    flash(request, "担当を引き取りました")
    return to(f"/orders/{o.id}")


@router.post("/orders/{order_id}/tracking/confirm")
def tracking_confirm(order_id: int, request: Request, carrier: str = Form(""), number: str = Form(""),
                     version: str = Form(""), user: User = Depends(current_user), db: Session = Depends(get_db)):
    """F6-9：形式チェック → 確認画面。"""
    o = _o(db, order_id)
    num = order_svc.validate_tracking(db, carrier, number)
    return render(request, "tracking_confirm.html", {"user": user, "o": o, "carrier": carrier.upper(), "number": num,
                                                     "carriers": order_svc.CARRIERS, "version": version})


@router.post("/orders/{order_id}/tracking")
def tracking_save(order_id: int, request: Request, carrier: str = Form(""), number: str = Form(""),
                  version: str = Form(""), user: User = Depends(current_user), db: Session = Depends(get_db)):
    o = _o(db, order_id)
    s = order_svc.add_shipment(db, user, o, carrier, number, form_int(version))
    res = get_order_adapter(db).upload_tracking(db, user, s)
    if res.status == "uploaded":
        order_svc.mark_ebay_uploaded(db, user, s)
    db.commit()
    flash(request, "追跡番号を登録しました。下の手順で Seller Hub に登録し、「eBayに登録済み」を押してください"
          if res.status != "uploaded" else "追跡番号をeBayに登録しました")
    return to(f"/orders/{o.id}#tracking")


@router.post("/orders/{order_id}/tracking/uploaded")
def tracking_uploaded(order_id: int, request: Request, user: User = Depends(current_user),
                      db: Session = Depends(get_db)):
    o = _o(db, order_id)
    s = o.active_shipment()
    if not s:
        raise ServiceError("追跡番号が登録されていません")
    order_svc.mark_ebay_uploaded(db, user, s)
    db.commit()
    flash(request, "eBayに登録済みとして記録しました")
    return to(f"/orders/{o.id}")


@router.post("/orders/{order_id}/tracking/delete")
def tracking_delete(order_id: int, request: Request, reason: str = Form(""), version: str = Form(""),
                    user: User = Depends(require_lv2), db: Session = Depends(get_db)):
    o = _o(db, order_id)
    order_svc.delete_shipment(db, user, o, reason, form_int(version))
    db.commit()
    flash(request, "追跡番号を削除しました")
    return to(f"/orders/{o.id}")


@router.post("/orders/{order_id}/tracking-event")
def tracking_event(order_id: int, request: Request, status: str = Form(""), note: str = Form(""),
                   user: User = Depends(current_user), db: Session = Depends(get_db)):
    o = _o(db, order_id)
    order_svc.add_tracking_event(db, user, o, status, note or None)
    db.commit()
    flash(request, f"配送状態を「{order_svc.TRACKING_STATUSES[status]}」で記録しました")
    return to(f"/orders/{o.id}")


@router.post("/orders/{order_id}/checklist")
def order_checklist(order_id: int, request: Request, key: str = Form(""), checked: str = Form(""),
                    user: User = Depends(current_user), db: Session = Depends(get_db)):
    o = _o(db, order_id)
    order_svc.toggle_checklist(db, user, o, key, checked == "1")
    db.commit()
    return to(f"/orders/{o.id}#checklist")


@router.post("/orders/{order_id}/case-open")
def order_case_open(order_id: int, request: Request, note: str = Form(""), version: str = Form(""),
                    user: User = Depends(current_user), db: Session = Depends(get_db)):
    o = _o(db, order_id)
    order_svc.open_case(db, user, o, note, form_int(version))
    db.commit()
    flash(request, "ケースを記録しました")
    return to(f"/orders/{o.id}")


@router.post("/orders/{order_id}/case-close")
def order_case_close(order_id: int, request: Request, to_status: str = Form("previous"), note: str = Form(""),
                     version: str = Form(""), user: User = Depends(require_lv2), db: Session = Depends(get_db)):
    o = _o(db, order_id)
    order_svc.close_case(db, user, o, to_status, note, form_int(version))
    db.commit()
    flash(request, "ケースの解決を記録しました")
    return to(f"/orders/{o.id}")


@router.post("/orders/{order_id}/status")
def order_status(order_id: int, request: Request, to_status: str = Form(""), reason: str = Form(""),
                 version: str = Form(""), user: User = Depends(require_lv2), db: Session = Depends(get_db)):
    o = _o(db, order_id)
    order_svc.set_status_manual(db, user, o, to_status, reason, form_int(version))
    db.commit()
    flash(request, "ステータスを変更しました")
    return to(f"/orders/{o.id}")


@router.post("/orders/{order_id}/edit")
async def order_edit(order_id: int, request: Request, user: User = Depends(require_lv2),
                     db: Session = Depends(get_db)):
    """注文データの修正（Lv2）。"""
    from app.services.errors import check_version
    o = _o(db, order_id)
    form = await request.form()
    check_version(o, form_int(form.get("version")))
    before, after = {}, {}
    for k in ("buyer_username", "ship_country", "ebay_order_id", "currency"):
        v = (form.get(k) or "").strip() or None
        if k == "ship_country" and v:
            v = v.upper()
        if v != getattr(o, k):
            before[k], after[k] = getattr(o, k), v
            setattr(o, k, v)
    amt = (form.get("total_amount") or "").strip()
    if amt:
        from decimal import Decimal
        if Decimal(amt) != o.total_amount:
            before["total_amount"], after["total_amount"] = o.total_amount, Decimal(amt)
            o.total_amount = Decimal(amt)
    for k in ("paid_at", "ship_by", "payment_due"):
        v = form.get(k)
        if v:
            dt = form_dt_jst(v)
            if dt != getattr(o, k):
                before[k], after[k] = getattr(o, k), dt
                setattr(o, k, dt)
    if after:
        reason = (form.get("reason") or "").strip()
        if not reason:
            raise ServiceError("理由の入力が必須です")
        o.version += 1
        audit.log(db, audit.Actor.user(user), "order", o.id, "order.edit", before=before, after=after, reason=reason)
        db.commit()
        flash(request, "注文データを修正しました")
    return to(f"/orders/{o.id}")
