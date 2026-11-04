from __future__ import annotations

from datetime import datetime

from fastapi import Request
from fastapi.responses import RedirectResponse

from app.timeutil import jst_to_utc_naive


def back(request: Request, default: str = "/") -> RedirectResponse:
    ref = request.headers.get("referer") or ""
    host = request.headers.get("host", "")
    if ref and host and host in ref:
        return RedirectResponse(ref, status_code=303)
    return RedirectResponse(default, status_code=303)


def to(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def form_int(v) -> int | None:
    try:
        return int(v) if v not in (None, "") else None
    except ValueError:
        return None


def form_dt_jst(v: str | None) -> datetime | None:
    """<input type=datetime-local> の値（JST）を naive UTC に。"""
    if not v:
        return None
    return jst_to_utc_naive(datetime.fromisoformat(v))
