"""Discord 通知（F4）。

app / worker は notifications と outbox に書くだけ。実際の投稿は bot コンテナが outbox を拾って行う。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import audit
from app.config import get_config
from app.models import Notification, Order, Outbox, User
from app.services import duty
from app.settings_store import get_setting
from app.timeutil import utcnow

CHANNELS = ("orders", "listing", "daily", "weekly", "system")


def _mentions(db: Session, mention: str) -> tuple[list[str], list[str], str]:
    """(discord user ids, role keys, notifications.target)"""
    users: list[User] = []
    roles: list[str] = []
    if mention in ("duty", "duty+lv2"):
        u = duty.duty_user_for(db)
        if u:
            users.append(u)
        else:
            roles.append("lv2")  # 不在の日・祝日は @Lv2
    if mention == "photographer":
        users = duty.role_users(db, "is_photographer") or []
        if not users:
            roles.append("lv2")
    if mention == "approver":
        users = duty.role_users(db, "is_price_approver") or []
        if not users:
            roles.append("lv2")
    if mention in ("lv2", "duty+lv2") and "lv2" not in roles:
        roles.append("lv2")
    if mention.startswith("user:"):
        u = db.get(User, int(mention.split(":", 1)[1]))
        if u:
            users.append(u)
    target = ",".join([f"user:{u.id}" for u in users] + [f"role:{r}" for r in roles]) or "none"
    return [u.discord_user_id for u in users], roles, target


def enqueue_post(db: Session, channel: str, content: str, *, order_id: int | None = None,
                 mention_user_ids: list[str] | None = None, mention_roles: list[str] | None = None,
                 notification_id: int | None = None, link_path: str | None = None, buttons: bool = False) -> Outbox:
    cfg = get_config()
    ob = Outbox(kind="post", payload={
        "channel": channel,
        "content": content,
        "order_id": order_id,
        "mention_user_ids": mention_user_ids or [],
        "mention_roles": mention_roles or [],
        "notification_id": notification_id,
        "link": (cfg.base_url.rstrip("/") + link_path) if link_path else None,
        "buttons": buttons,
    })
    db.add(ob)
    db.flush()
    return ob


def notify(db: Session, event_type: str, target_type: str, target_id: int, message: str, *,
           channel: str = "orders", mention: str = "duty", dedupe_key: str = "",
           escalate_minutes: int | None | str = "default", escalate_at: datetime | None = None,
           link_path: str | None = None, buttons: bool = True) -> Notification | None:
    """同じ対象・同じイベント（・同じ dedupe_key）は1回だけ通知する（F4-5）。"""
    existing = db.scalar(select(Notification).where(
        Notification.target_type == target_type, Notification.target_id == target_id,
        Notification.event_type == event_type, Notification.dedupe_key == dedupe_key))
    if existing is not None:
        return None
    if escalate_minutes == "default":
        escalate_minutes = (get_setting(db, "escalation_minutes") or {}).get(event_type)
    now = utcnow()
    if escalate_at is None and isinstance(escalate_minutes, (int, float)):
        escalate_at = now + timedelta(minutes=escalate_minutes)
    user_ids, roles, target = _mentions(db, mention)
    n = Notification(target_type=target_type, target_id=target_id, event_type=event_type,
                     dedupe_key=dedupe_key, target=target, channel=channel, message=message,
                     sent_at=now, escalate_at=escalate_at)
    try:
        with db.begin_nested():
            db.add(n)
            db.flush()
    except IntegrityError:
        return None
    order_id = target_id if target_type == "order" else None
    enqueue_post(db, channel, message, order_id=order_id, mention_user_ids=user_ids, mention_roles=roles,
                 notification_id=n.id, link_path=link_path, buttons=buttons)
    audit.log(db, audit.Actor.system(), "notification", n.id, "notify.sent",
              after={"event": event_type, "target": target, "channel": channel, "about": f"{target_type}:{target_id}"})
    return n


def system_alert(db: Session, message: str, *, event_type: str = "system", dedupe_key: str | None = None) -> None:
    """#system への通知（Lv2全員にメンション）。"""
    if dedupe_key is not None:
        notify(db, event_type, "system", 0, message, channel="system", mention="lv2",
               dedupe_key=dedupe_key, escalate_minutes=None, buttons=False)
    else:
        enqueue_post(db, "system", message, mention_roles=["lv2"])


def acknowledge(db: Session, n: Notification, user: User | None, actor: audit.Actor, *, via: str = "web") -> bool:
    if n.acknowledged_at is not None:
        return False
    n.acknowledged_at = utcnow()
    n.acknowledged_by = user.id if user else None
    audit.log(db, actor, "notification", n.id, "notify.acknowledged",
              after={"event": n.event_type, "via": via, "about": f"{n.target_type}:{n.target_id}"})
    return True


def resolve(db: Session, target_type: str, target_id: int, event_types: list[str]) -> None:
    """条件が解消した通知を自動で対応済みにする（エスカレーションを止める）。"""
    rows = db.scalars(select(Notification).where(
        Notification.target_type == target_type, Notification.target_id == target_id,
        Notification.event_type.in_(event_types), Notification.acknowledged_at.is_(None))).all()
    for n in rows:
        acknowledge(db, n, None, audit.Actor.system(), via="auto")


def claim_order(db: Session, order: Order, user: User, actor: audit.Actor) -> None:
    before = order.assignee_id
    order.assignee_id = user.id
    order.version += 1
    audit.log(db, actor, "order", order.id, "order.claim", before={"assignee_id": before},
              after={"assignee_id": user.id})
    enqueue_post(db, "orders", f"{user.display_name} さんが担当を引き取りました。", order_id=order.id)


def run_escalations(db: Session, now: datetime | None = None) -> int:
    now = now or utcnow()
    rows = db.scalars(select(Notification).where(
        Notification.escalate_at.is_not(None), Notification.escalate_at <= now,
        Notification.acknowledged_at.is_(None), Notification.escalated_at.is_(None))).all()
    for n in rows:
        n.escalated_at = now
        content = f"【エスカレーション】未対応のまま時間が経過しました：{n.message}"
        link = f"/orders/{n.target_id}" if n.target_type == "order" else (
            f"/items/{n.target_id}" if n.target_type == "item" else None)
        enqueue_post(db, n.channel, content, order_id=n.target_id if n.target_type == "order" else None,
                     mention_roles=["lv2"], notification_id=n.id, link_path=link, buttons=True)
        audit.log(db, audit.Actor.system(), "notification", n.id, "notify.escalated",
                  after={"event": n.event_type, "about": f"{n.target_type}:{n.target_id}"})
    return len(rows)
