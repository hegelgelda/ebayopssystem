"""曜日担当（F5）と撮影・承認担当（4.1）の判定。"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DutyOverride, DutySchedule, User
from app.settings_store import get_setting
from app.timeutil import is_jp_holiday, today_jst


def duty_user_for(db: Session, d: date | None = None) -> User | None:
    """その日（JST）の担当者。None は不在（→ Lv2ロールにメンション）。"""
    d = d or today_jst()
    ov = db.get(DutyOverride, d)
    if ov is not None:
        return ov.user if ov.user and ov.user.is_active else None
    if is_jp_holiday(d) and not get_setting(db, "duty_on_holiday"):
        return None
    row = db.get(DutySchedule, d.weekday())
    if row and row.user and row.user.is_active:
        return row.user
    return None


def role_users(db: Session, flag: str, d: date | None = None) -> list[User]:
    """撮影担当（is_photographer）・価格承認者（is_price_approver）。未設定なら曜日担当が兼ねる。"""
    users = db.scalars(select(User).where(getattr(User, flag).is_(True), User.is_active.is_(True))).all()
    if users:
        return list(users)
    u = duty_user_for(db, d)
    return [u] if u else []


def lv2_users(db: Session) -> list[User]:
    return list(db.scalars(select(User).where(User.level >= 2, User.is_active.is_(True))).all())


def next_listing_date(db: Session, from_date: date | None = None) -> date | None:
    weekdays = get_setting(db, "listing_weekdays") or []
    if not weekdays:
        return None
    d = from_date or today_jst()
    for i in range(0, 8):
        cand = d + timedelta(days=i)
        if cand.weekday() in weekdays:
            return cand
    return None
