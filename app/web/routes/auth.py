"""Discord OAuth2 ログイン（scope: identify）。許可リスト（users テーブル）にいる人だけ入れる。"""
from __future__ import annotations

import secrets
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit
from app.config import get_config
from app.db import get_db
from app.models import User
from app.timeutil import utcnow
from app.web.deps import render

router = APIRouter()

DISCORD_AUTHORIZE = "https://discord.com/oauth2/authorize"
DISCORD_TOKEN = "https://discord.com/api/oauth2/token"
DISCORD_ME = "https://discord.com/api/users/@me"


def _redirect_uri() -> str:
    return get_config().base_url.rstrip("/") + "/auth/callback"


@router.get("/login")
def login_page(request: Request):
    cfg = get_config()
    return render(request, "login.html", {"dev_login": cfg.is_dev and cfg.dev_login,
                                          "discord_ready": bool(cfg.discord_client_id)})


@router.get("/auth/discord")
def auth_discord(request: Request):
    cfg = get_config()
    state = secrets.token_urlsafe(24)
    request.session["oauth_state"] = state
    q = urlencode({"client_id": cfg.discord_client_id, "redirect_uri": _redirect_uri(), "response_type": "code",
                   "scope": "identify", "state": state, "prompt": "none"})
    return RedirectResponse(f"{DISCORD_AUTHORIZE}?{q}", status_code=302)


def _login(request: Request, db: Session, user: User, via: str):
    request.session.clear()
    request.session["uid"] = user.id
    user.last_login_at = utcnow()
    audit.log(db, audit.Actor.user(user), "user", user.id, "user.login", after={"via": via})
    db.commit()
    nxt = request.query_params.get("next") or "/"
    return RedirectResponse(nxt if nxt.startswith("/") else "/", status_code=303)


@router.get("/auth/callback")
def auth_callback(request: Request, code: str = "", state: str = "", db: Session = Depends(get_db)):
    cfg = get_config()
    if not code or not state or state != request.session.pop("oauth_state", None):
        return render(request, "login.html", {"error": "ログインに失敗しました（state不一致）"}, 400)
    try:
        tok = httpx.post(DISCORD_TOKEN, data={
            "client_id": cfg.discord_client_id, "client_secret": cfg.discord_client_secret,
            "grant_type": "authorization_code", "code": code, "redirect_uri": _redirect_uri()},
            headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=10)
        tok.raise_for_status()
        me = httpx.get(DISCORD_ME, headers={"Authorization": f"Bearer {tok.json()['access_token']}"}, timeout=10)
        me.raise_for_status()
        discord_id = str(me.json()["id"])
    except Exception:  # noqa: BLE001
        return render(request, "login.html", {"error": "Discordとの通信に失敗しました"}, 502)
    user = db.scalar(select(User).where(User.discord_user_id == discord_id))
    if user is None or not user.is_active:
        audit.log(db, audit.Actor("system"), "user", discord_id, "user.login_denied")
        db.commit()
        return render(request, "login.html", {"error": "このDiscordアカウントはツールに登録されていません。Lv2の担当者に招待を依頼してください。"}, 403)
    return _login(request, db, user, "discord")


@router.post("/auth/dev-login")
def dev_login(request: Request, discord_user_id: str = Form(""), db: Session = Depends(get_db)):
    """開発・動作確認用。APP_ENV=dev かつ DEV_LOGIN=1 のときだけ有効。"""
    cfg = get_config()
    if not (cfg.is_dev and cfg.dev_login):
        return render(request, "login.html", {"error": "開発用ログインは無効です"}, 404)
    user = db.scalar(select(User).where(User.discord_user_id == discord_user_id, User.is_active.is_(True)))
    if user is None:
        return render(request, "login.html", {"error": "ユーザーがいません", "dev_login": True}, 403)
    return _login(request, db, user, "dev")


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
