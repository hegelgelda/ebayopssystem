"""ダッシュボード（F6-1）、通知の対応、設定（F6-11）、ユーザー管理（F6-12）、操作ログ（F6-13）。"""
from __future__ import annotations

import json
from datetime import date

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit
from app.db import get_db
from app.models import AuditLog, DutyOverride, DutySchedule, Notification, Order, User
from app.services import duty, items as item_svc, listing as listing_svc, notify, orders as order_svc, users as user_svc
from app.services.errors import NotFound, ServiceError
from app.settings_store import DEFAULTS, get_setting, set_setting
from app.timeutil import today_jst
from app.web.deps import current_user, flash, render, require_lv2
from app.web.routes.common import back, to

router = APIRouter()


@router.get("/")
def dashboard(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    orders = order_svc.todays_orders(db)
    rows = [(o, *order_svc.deadline_of(o)) for o in orders]
    pending_notes = db.scalars(select(Notification).where(
        Notification.acknowledged_at.is_(None), Notification.target_type != "system",
        Notification.escalate_at.is_not(None)).order_by(Notification.sent_at.desc()).limit(30)).all()
    return render(request, "dashboard.html", {
        "user": user, "rows": rows, "counts": item_svc.status_counts(db), "stale": item_svc.stale_items(db),
        "next_listing": duty.next_listing_date(db), "duty_user": duty.duty_user_for(db),
        "photographers": duty.role_users(db, "is_photographer"),
        "approvers": duty.role_users(db, "is_price_approver"), "notes": pending_notes, "today": today_jst()})


@router.post("/notifications/{nid}/ack")
def notification_ack(nid: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    n = db.get(Notification, nid)
    if not n:
        raise NotFound("通知が見つかりません")
    notify.acknowledge(db, n, user, audit.Actor.user(user), via="web")
    if n.target_type == "order":
        notify.enqueue_post(db, "orders", f"✅ {user.display_name} さんが対応済みにしました。", order_id=n.target_id)
    db.commit()
    flash(request, "対応済みにしました")
    return back(request)


@router.post("/notifications/{nid}/claim")
def notification_claim(nid: int, request: Request, user: User = Depends(current_user),
                       db: Session = Depends(get_db)):
    n = db.get(Notification, nid)
    if not n:
        raise NotFound("通知が見つかりません")
    if n.target_type == "order":
        o = db.get(Order, n.target_id)
        if o:
            notify.claim_order(db, o, user, audit.Actor.user(user))
    notify.acknowledge(db, n, user, audit.Actor.user(user), via="web-claim")
    db.commit()
    flash(request, "担当を引き取りました")
    return back(request)


# ---- 設定（Lv2） ----

JSON_KEYS = ["escalation_minutes", "checklist_items", "tracking_regex", "price_rule", "fees", "listing_defaults",
             "stale_days", "digest_times_jst"]


@router.get("/settings")
def settings_page(request: Request, user: User = Depends(require_lv2), db: Session = Depends(get_db)):
    users = db.scalars(select(User).where(User.is_active.is_(True)).order_by(User.display_name)).all()
    schedule = {r.weekday: r.user_id for r in db.scalars(select(DutySchedule))}
    overrides = db.scalars(select(DutyOverride).where(DutyOverride.date >= date.today())
                           .order_by(DutyOverride.date)).all()
    json_values = {k: json.dumps(get_setting(db, k), ensure_ascii=False, indent=2) for k in JSON_KEYS}
    templates = {k: listing_svc.get_template(db, k) for k in ("title", "description", "item_specifics")}
    db.commit()
    return render(request, "settings.html", {
        "user": user, "users": users, "schedule": schedule, "overrides": overrides, "json_values": json_values,
        "listing_weekdays": get_setting(db, "listing_weekdays"), "duty_on_holiday": get_setting(db, "duty_on_holiday"),
        "show_pp": get_setting(db, "show_purchase_price_to_lv1"), "adapter": get_setting(db, "ebay_adapter"),
        "templates": templates, "ship_due_hours": (get_setting(db, "ship_due_notify_hour_jst"),
                                                   get_setting(db, "ship_due_escalate_hour_jst"))})


def _save(db: Session, user: User, key: str, value) -> None:
    before, after = set_setting(db, key, value, user.id)
    if before != after:
        audit.log(db, audit.Actor.user(user), "setting", key, "setting.update", before={"value": before},
                  after={"value": after})


@router.post("/settings/json/{key}")
def settings_json(key: str, request: Request, value: str = Form(...), user: User = Depends(require_lv2),
                  db: Session = Depends(get_db)):
    if key not in JSON_KEYS:
        raise NotFound("不明な設定です")
    try:
        v = json.loads(value)
    except json.JSONDecodeError as e:
        raise ServiceError(f"JSONの書式エラー：{e}")
    if type(v) is not type(DEFAULTS[key]):
        raise ServiceError("値の型が初期値と異なります")
    if key == "tracking_regex":
        import re
        for k, rx in v.items():
            re.compile(rx)
    _save(db, user, key, v)
    db.commit()
    flash(request, f"{key} を保存しました")
    return to("/settings#" + key)


@router.post("/settings/general")
async def settings_general(request: Request, user: User = Depends(require_lv2), db: Session = Depends(get_db)):
    form = await request.form()
    _save(db, user, "listing_weekdays", sorted(int(x) for x in form.getlist("listing_weekdays")))
    _save(db, user, "duty_on_holiday", form.get("duty_on_holiday") == "1")
    _save(db, user, "show_purchase_price_to_lv1", form.get("show_pp") == "1")
    _save(db, user, "ship_due_notify_hour_jst", int(form.get("ship_due_notify_hour_jst") or 9))
    _save(db, user, "ship_due_escalate_hour_jst", int(form.get("ship_due_escalate_hour_jst") or 12))
    adapter = form.get("ebay_adapter") or "manual"
    if adapter != "manual":
        raise ServiceError("api アダプタは Phase 3 で利用可能になります")
    _save(db, user, "ebay_adapter", adapter)
    db.commit()
    flash(request, "保存しました")
    return to("/settings")


@router.post("/settings/duty")
async def settings_duty(request: Request, user: User = Depends(require_lv2), db: Session = Depends(get_db)):
    form = await request.form()
    before = {r.weekday: r.user_id for r in db.scalars(select(DutySchedule))}
    after = {}
    for wd in range(7):
        v = form.get(f"wd{wd}") or ""
        uid = int(v) if v else None
        row = db.get(DutySchedule, wd)
        if row is None:
            row = DutySchedule(weekday=wd)
            db.add(row)
        row.user_id = uid
        after[wd] = uid
    audit.log(db, audit.Actor.user(user), "setting", "duty_schedule", "duty.update", before=before, after=after)
    db.commit()
    flash(request, "曜日担当表を保存しました")
    return to("/settings#duty")


@router.post("/settings/override")
def settings_override(request: Request, d: str = Form(...), user_id: str = Form(""), delete: str = Form(""),
                      user: User = Depends(require_lv2), db: Session = Depends(get_db)):
    day = date.fromisoformat(d)
    row = db.get(DutyOverride, day)
    before = {"user_id": row.user_id} if row else None
    if delete:
        if row:
            db.delete(row)
        after = None
    else:
        if row is None:
            row = DutyOverride(date=day)
            db.add(row)
        row.user_id = int(user_id) if user_id else None
        after = {"user_id": row.user_id}
    audit.log(db, audit.Actor.user(user), "setting", f"duty_override:{day}", "duty.override", before=before,
              after=after)
    db.commit()
    flash(request, "臨時差し替えを保存しました")
    return to("/settings#duty")


@router.post("/settings/template/{key}")
def settings_template(key: str, request: Request, body: str = Form(...), user: User = Depends(require_lv2),
                      db: Session = Depends(get_db)):
    listing_svc.update_template(db, user, key, body)
    db.commit()
    flash(request, f"テンプレート（{key}）を保存しました")
    return to("/settings#templates")


# ---- ユーザー管理（Lv2） ----

@router.get("/users")
def users_page(request: Request, user: User = Depends(require_lv2), db: Session = Depends(get_db)):
    users = db.scalars(select(User).order_by(User.is_active.desc(), User.level.desc(), User.display_name)).all()
    return render(request, "users.html", {"user": user, "users": users})


@router.post("/users")
def users_create(request: Request, discord_user_id: str = Form(...), display_name: str = Form(""),
                 level: int = Form(1), user: User = Depends(require_lv2), db: Session = Depends(get_db)):
    u = user_svc.create_user(db, discord_user_id, display_name, level, actor_user=user)
    db.commit()
    flash(request, f"{u.display_name} を招待しました（Lv{u.level}）")
    return to("/users")


@router.post("/users/{uid}/level")
def users_level(uid: int, request: Request, level: int = Form(...), user: User = Depends(require_lv2),
                db: Session = Depends(get_db)):
    user_svc.change_level(db, user, user_svc.get_user(db, uid), level)
    db.commit()
    flash(request, "権限を変更しました")
    return to("/users")


@router.post("/users/{uid}/deactivate")
def users_deactivate(uid: int, request: Request, user: User = Depends(require_lv2), db: Session = Depends(get_db)):
    user_svc.deactivate(db, user, user_svc.get_user(db, uid))
    db.commit()
    flash(request, "無効化しました")
    return to("/users")


@router.post("/users/{uid}/reactivate")
def users_reactivate(uid: int, request: Request, user: User = Depends(require_lv2), db: Session = Depends(get_db)):
    t = user_svc.get_user(db, uid)
    user_svc.create_user(db, t.discord_user_id, t.display_name, t.level, actor_user=user)
    db.commit()
    flash(request, "再有効化しました")
    return to("/users")


@router.post("/users/{uid}/roles")
def users_roles(uid: int, request: Request, is_photographer: str = Form(""), is_price_approver: str = Form(""),
                display_name: str = Form(""), user: User = Depends(require_lv2), db: Session = Depends(get_db)):
    user_svc.set_roles(db, user, user_svc.get_user(db, uid), is_photographer == "1", is_price_approver == "1",
                       display_name or None)
    db.commit()
    flash(request, "保存しました")
    return to("/users")


# ---- 操作ログ ----

@router.get("/audit")
def audit_page(request: Request, target_type: str = "", target_id: str = "", action: str = "", actor: str = "",
               page: int = 1, user: User = Depends(current_user), db: Session = Depends(get_db)):
    stmt = select(AuditLog)
    if target_type:
        stmt = stmt.where(AuditLog.target_type == target_type)
    if target_id:
        stmt = stmt.where(AuditLog.target_id == target_id)
    if action:
        stmt = stmt.where(AuditLog.action.like(f"%{action}%"))
    if actor:
        if actor in ("system", "bot", "agent"):
            stmt = stmt.where(AuditLog.actor_type == actor)
        else:
            stmt = stmt.where(AuditLog.actor_id == int(actor), AuditLog.actor_type.in_(["user", "bot"]))
    per = 100
    rows = db.scalars(stmt.order_by(AuditLog.id.desc()).offset((page - 1) * per).limit(per + 1)).all()
    users = {u.id: u for u in db.scalars(select(User))}
    return render(request, "audit.html", {"user": user, "rows": rows[:per], "has_next": len(rows) > per,
                                          "page": page, "users": users, "show_sensitive": user.level >= 2,
                                          "f": {"target_type": target_type, "target_id": target_id,
                                                "action": action, "actor": actor}})

