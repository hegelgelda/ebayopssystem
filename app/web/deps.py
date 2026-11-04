from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import Depends, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import ItemStatus, OrderStatus, User
from app.services.errors import PermissionDenied
from app.timeutil import WEEKDAY_JP, fmt_jst, remaining_label

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _money(v: Any, cur: str | None = None) -> str:
    if v is None or v == "":
        return "—"
    try:
        d = Decimal(str(v))
    except Exception:  # noqa: BLE001
        return str(v)
    if cur == "JPY":
        return f"¥{int(d):,}"
    s = f"{d:,.2f}"
    return f"${s}" if cur in (None, "USD") else f"{cur} {s}"


templates.env.policies["json.dumps_kwargs"] = {"ensure_ascii": False, "sort_keys": True}
templates.env.filters["jst"] = fmt_jst
templates.env.filters["money"] = _money
templates.env.filters["remaining"] = lambda dt: remaining_label(dt)
SENSITIVE_KEYS = {"purchase_price_jpy", "purchase_date", "purchase_jpy", "gross_profit_jpy", "revenue_jpy",
                  "fees_jpy", "duty_jpy", "shipping_jpy"}


def _redact(data: Any, show: bool) -> Any:
    """仕入値・粗利を見られない利用者向けに操作ログの値を伏せる。"""
    if show or not isinstance(data, dict):
        return data
    return {k: ("***" if k in SENSITIVE_KEYS else v) for k, v in data.items()}


templates.env.filters["redact"] = _redact
templates.env.globals["ITEM_LABELS"] = ItemStatus.LABELS
templates.env.globals["ORDER_LABELS"] = OrderStatus.LABELS
templates.env.globals["WEEKDAY_JP"] = WEEKDAY_JP


class NotAuthenticated(Exception):
    pass


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    uid = request.session.get("uid")
    if not uid:
        raise NotAuthenticated()
    user = db.get(User, uid)
    if user is None or not user.is_active:
        request.session.clear()
        raise NotAuthenticated()
    return user


def require_lv2(user: User = Depends(current_user)) -> User:
    if user.level < 2:
        raise PermissionDenied("この操作はLv2のみ可能です")
    return user


def flash(request: Request, message: str, kind: str = "ok") -> None:
    # 入れ子のリストを書き換えるだけではセッションが保存されないので、キーごと代入する
    request.session["flash"] = [*request.session.get("flash", []), {"kind": kind, "message": message}]


def render(request: Request, name: str, ctx: dict[str, Any], status_code: int = 200):
    flashes = request.session.pop("flash", []) if "session" in request.scope else []
    base = {"request": request, "flashes": flashes, "user": ctx.get("user")}
    base.update(ctx)
    return templates.TemplateResponse(request, name, base, status_code=status_code)
