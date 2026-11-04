"""ユーザー管理（4.1〜4.3）。"""
from __future__ import annotations

import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import audit
from app.models import Outbox, User
from app.services import notify
from app.services.errors import NotFound, PermissionDenied, ServiceError


def _require_lv2(user: User) -> None:
    if user.level < 2:
        raise PermissionDenied("ユーザー管理はLv2のみ可能です")


def active_lv2_count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(User).where(User.level >= 2, User.is_active.is_(True))) or 0


def _announce(db: Session, text: str) -> None:
    notify.enqueue_post(db, "system", f"👤 {text}", mention_roles=["lv2"])
    db.add(Outbox(kind="sync_roles", payload={}))


def create_user(db: Session, discord_user_id: str, display_name: str, level: int,
                actor_user: User | None = None) -> User:
    """actor_user=None は初期設定CLIからの作成。"""
    if actor_user is not None:
        _require_lv2(actor_user)
    discord_user_id = (discord_user_id or "").strip()
    if not re.fullmatch(r"\d{15,21}", discord_user_id):
        raise ServiceError("DiscordのユーザーIDは15〜21桁の数字です")
    if level not in (1, 2):
        raise ServiceError("権限レベルは1か2です")
    existing = db.scalar(select(User).where(User.discord_user_id == discord_user_id))
    if existing:
        if existing.is_active:
            raise ServiceError("このDiscordユーザーは登録済みです")
        existing.is_active = True
        existing.level = level
        existing.display_name = display_name or existing.display_name
        u = existing
    else:
        u = User(discord_user_id=discord_user_id, display_name=display_name or discord_user_id, level=level)
        db.add(u)
    db.flush()
    actor = audit.Actor.user(actor_user) if actor_user else audit.Actor("system", None, "cli")
    audit.log(db, actor, "user", u.id, "user.create",
              after={"discord_user_id": discord_user_id, "display_name": u.display_name, "level": level})
    _announce(db, f"ユーザー追加：{u.display_name}（Lv{level}）／操作者：{actor.name}")
    return u


def get_user(db: Session, user_id: int) -> User:
    u = db.get(User, user_id)
    if not u:
        raise NotFound("ユーザーが見つかりません")
    return u


def change_level(db: Session, actor_user: User, target: User, level: int) -> None:
    _require_lv2(actor_user)
    if target.id == actor_user.id:
        raise PermissionDenied("自分自身の権限は変更できません")
    if level not in (1, 2):
        raise ServiceError("権限レベルは1か2です")
    if target.level == level:
        return
    if target.level >= 2 and level < 2 and target.is_active and active_lv2_count(db) <= 1:
        raise ServiceError("最後のLv2ユーザーは降格できません")
    before = target.level
    target.level = level
    audit.log(db, audit.Actor.user(actor_user), "user", target.id, "user.level",
              before={"level": before}, after={"level": level})
    _announce(db, f"権限変更：{target.display_name} Lv{before} → Lv{level}／操作者：{actor_user.display_name}")


def deactivate(db: Session, actor_user: User, target: User) -> None:
    _require_lv2(actor_user)
    if target.id == actor_user.id:
        raise PermissionDenied("自分自身は無効化できません")
    if not target.is_active:
        return
    if target.level >= 2 and active_lv2_count(db) <= 1:
        raise ServiceError("最後のLv2ユーザーは削除できません")
    target.is_active = False
    audit.log(db, audit.Actor.user(actor_user), "user", target.id, "user.deactivate",
              before={"is_active": True}, after={"is_active": False})
    _announce(db, f"ユーザー無効化：{target.display_name}／操作者：{actor_user.display_name}")


def set_roles(db: Session, actor_user: User, target: User, photographer: bool, approver: bool,
              display_name: str | None = None) -> None:
    _require_lv2(actor_user)
    before = {"is_photographer": target.is_photographer, "is_price_approver": target.is_price_approver,
              "display_name": target.display_name}
    target.is_photographer, target.is_price_approver = photographer, approver
    if display_name:
        target.display_name = display_name.strip()
    after = {"is_photographer": photographer, "is_price_approver": approver, "display_name": target.display_name}
    if before != after:
        audit.log(db, audit.Actor.user(actor_user), "user", target.id, "user.update", before=before, after=after)
