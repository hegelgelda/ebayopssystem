"""操作ログ（F7）。全ての状態変更はここを通して記録する。"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.models import AuditLog


def _jsonable(v: Any) -> Any:
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


class Actor:
    def __init__(self, actor_type: str, actor_id: int | None = None, name: str = ""):
        self.type = actor_type
        self.id = actor_id
        self.name = name

    @classmethod
    def user(cls, user) -> "Actor":
        return cls("user", user.id, user.display_name)

    @classmethod
    def system(cls) -> "Actor":
        return cls("system", None, "system")

    @classmethod
    def bot(cls, user) -> "Actor":
        return cls("bot", user.id if user else None, user.display_name if user else "bot")


def log(db: Session, actor: Actor, target_type: str, target_id: Any, action: str,
        before: dict | None = None, after: dict | None = None, reason: str | None = None) -> AuditLog:
    entry = AuditLog(
        actor_type=actor.type, actor_id=actor.id, target_type=target_type,
        target_id=str(target_id) if target_id is not None else None, action=action,
        before_json=_jsonable(before) if before is not None else None,
        after_json=_jsonable(after) if after is not None else None, reason=reason,
    )
    db.add(entry)
    db.flush()
    return entry
