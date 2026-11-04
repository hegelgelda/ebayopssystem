"""外部API呼び出しの成否を記録し、3回連続で失敗したら #system に通知する（7章 障害の検知）。"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import ApiHealth
from app.timeutil import utcnow

THRESHOLD = 3


def record(db: Session, provider: str, *, ok: bool, error: str | None = None) -> None:
    row = db.get(ApiHealth, provider)
    if row is None:
        row = ApiHealth(provider=provider, consecutive_failures=0, alerted=False)
        db.add(row)
    if ok:
        if row.alerted:
            from app.services.notify import enqueue_post
            enqueue_post(db, "system", f"✅ {provider} への接続が回復しました。", mention_roles=[])
        row.consecutive_failures = 0
        row.last_success_at = utcnow()
        row.alerted = False
    else:
        row.consecutive_failures = (row.consecutive_failures or 0) + 1
        row.last_error = (error or "")[:500]
        row.last_failure_at = utcnow()
        if row.consecutive_failures >= THRESHOLD and not row.alerted:
            from app.services.notify import enqueue_post
            enqueue_post(db, "system",
                         f"⚠️ {provider} の呼び出しが{row.consecutive_failures}回連続で失敗しています：{row.last_error}",
                         mention_roles=["lv2"])
            row.alerted = True
    db.flush()
