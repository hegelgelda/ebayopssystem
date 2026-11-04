"""為替（Frankfurter API）。日次で USD/JPY を取得して fx_rates に保存する。"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import FxRate
from app.services import health
from app.settings_store import get_setting

FRANKFURTER = "https://api.frankfurter.dev/v1"


def fetch_usdjpy(db: Session, d: date | None = None) -> FxRate | None:
    path = d.isoformat() if d else "latest"
    try:
        r = httpx.get(f"{FRANKFURTER}/{path}", params={"from": "USD", "to": "JPY"}, timeout=15,
                      follow_redirects=True)
        r.raise_for_status()
        js = r.json()
        rate_date = date.fromisoformat(js["date"])
        rate = Decimal(str(js["rates"]["JPY"]))
    except Exception as e:  # noqa: BLE001
        health.record(db, "frankfurter", ok=False, error=str(e))
        return None
    health.record(db, "frankfurter", ok=True)
    row = db.get(FxRate, (rate_date, "USDJPY"))
    if row is None:
        row = FxRate(date=rate_date, pair="USDJPY", rate=rate, source="frankfurter")
        db.add(row)
    else:
        row.rate = rate
    db.flush()
    return row


def usdjpy_on(db: Session, d: date | datetime | None = None) -> tuple[Decimal, str]:
    """その日以前で最も新しいレート。無ければ設定のフォールバック値。"""
    if isinstance(d, datetime):
        d = d.date()
    q = select(FxRate).where(FxRate.pair == "USDJPY")
    if d:
        q = q.where(FxRate.date <= d)
    row = db.scalar(q.order_by(FxRate.date.desc()).limit(1))
    if row:
        return Decimal(row.rate), f"{row.source}:{row.date.isoformat()}"
    fb = (get_setting(db, "fees") or {}).get("fx_fallback_usdjpy", 150)
    return Decimal(str(fb)), "settings:fx_fallback_usdjpy"
