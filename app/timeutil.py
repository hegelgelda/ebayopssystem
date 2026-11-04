"""時刻の扱い。内部はUTC（naive datetime を UTC とみなして保存）、表示と期限判定はJST。"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import jpholiday

JST = ZoneInfo("Asia/Tokyo")
UTC = timezone.utc


def utcnow() -> datetime:
    """naive UTC。DB保存用。"""
    return datetime.now(UTC).replace(tzinfo=None)


def to_jst(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(JST)


def jst_to_utc_naive(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=JST)
    return dt.astimezone(UTC).replace(tzinfo=None)


def today_jst(now_utc: datetime | None = None) -> date:
    now_utc = now_utc or utcnow()
    return to_jst(now_utc).date()  # type: ignore[union-attr]


def is_jp_holiday(d: date) -> bool:
    return jpholiday.is_holiday(d)


def is_business_day(d: date) -> bool:
    return d.weekday() < 5 and not is_jp_holiday(d)


def add_business_days(d: date, n: int) -> date:
    cur = d
    added = 0
    while added < n:
        cur += timedelta(days=1)
        if is_business_day(cur):
            added += 1
    return cur


def ship_by_from_paid(paid_at_utc: datetime, business_days: int = 2) -> datetime:
    """F3-1：支払完了日時から日本の2営業日後の23:59 JST（UTCで返す）。"""
    paid_jst_date = to_jst(paid_at_utc).date()  # type: ignore[union-attr]
    due = add_business_days(paid_jst_date, business_days)
    return jst_to_utc_naive(datetime.combine(due, time(23, 59), tzinfo=JST))


def fmt_jst(dt: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    if dt is None:
        return ""
    return to_jst(dt).strftime(fmt)  # type: ignore[union-attr]


def remaining_label(deadline: datetime | None, now: datetime | None = None) -> tuple[str, bool]:
    """(表示文字列, 超過しているか)"""
    if deadline is None:
        return ("", False)
    now = now or utcnow()
    delta = deadline - now
    overdue = delta.total_seconds() < 0
    secs = abs(int(delta.total_seconds()))
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        s = f"{d}日{h}時間"
    elif h:
        s = f"{h}時間{m}分"
    else:
        s = f"{m}分"
    return (f"{s}超過" if overdue else f"残り{s}", overdue)


WEEKDAY_JP = ["月", "火", "水", "木", "金", "土", "日"]
