from __future__ import annotations

import logging
import mimetypes
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_config
from app.db import SessionLocal
from app.logging_setup import setup_logging
from app.services.errors import PermissionDenied, ServiceError, VersionConflict
from app.web.deps import NotAuthenticated, flash, render
from app.web.routes import admin, auth, items, orders, pricing_listing

STATIC_DIR = Path(__file__).parent / "static"
# Safari の WebAssembly 読み込みには application/wasm が必要（Python のバージョンに依らず登録しておく）
mimetypes.add_type("application/wasm", ".wasm")
log = logging.getLogger(__name__)


def _wants_json(request: Request) -> bool:
    return request.url.path.startswith("/api/") or "application/json" in request.headers.get("accept", "")


def create_app() -> FastAPI:
    cfg = get_config()
    cfg.ensure_dirs()
    setup_logging("app")
    app = FastAPI(title="eBay運用ツール", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SessionMiddleware, secret_key=cfg.session_secret, session_cookie="ops_session",
                       max_age=60 * 60 * 24 * 30, same_site="lax", https_only=cfg.base_url.startswith("https"))

    @app.exception_handler(NotAuthenticated)
    async def _unauth(request: Request, exc: NotAuthenticated):
        if _wants_json(request) or request.method != "GET":
            return JSONResponse({"error": "ログインが必要です"}, status_code=401)
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)

    @app.exception_handler(PermissionDenied)
    async def _forbidden(request: Request, exc: PermissionDenied):
        log.info("permission denied: %s %s", request.url.path, exc.message)
        if _wants_json(request):
            return JSONResponse({"error": exc.message}, status_code=403)
        return render(request, "error.html", {"message": exc.message, "code": 403}, 403)

    @app.exception_handler(ServiceError)
    async def _service_error(request: Request, exc: ServiceError):
        if _wants_json(request):
            return JSONResponse({"error": exc.message}, status_code=exc.status_code)
        if request.method == "POST":
            flash(request, exc.message, "warn" if isinstance(exc, VersionConflict) else "error")
            ref = request.headers.get("referer") or "/"
            return RedirectResponse(ref, status_code=303)
        return render(request, "error.html", {"message": exc.message, "code": exc.status_code}, exc.status_code)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    # 本番は Caddy が /img/ を直接配信する。app 単体で動かすとき用に同じパスで配信する
    app.mount("/img", StaticFiles(directory=cfg.public_images_dir, check_dir=False), name="img")

    @app.get("/healthz")
    def healthz():
        with SessionLocal() as s:
            s.execute(text("select 1"))
        return PlainTextResponse("ok")

    @app.get("/manifest.webmanifest")
    def manifest():
        return FileResponse(STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/sw.js")
    def sw():
        return FileResponse(STATIC_DIR / "sw.js", media_type="application/javascript",
                            headers={"Service-Worker-Allowed": "/"})

    for r in (auth.router, admin.router, items.router, pricing_listing.router, orders.router):
        app.include_router(r)
    return app


app = create_app()
