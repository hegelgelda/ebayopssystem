from __future__ import annotations

import io
import os

import pytest
from PIL import Image

os.environ.setdefault("APP_ENV", "test")
os.environ["DEV_LOGIN"] = "1"
os.environ["ENABLE_REMBG"] = "0"
os.environ["BASE_URL"] = "https://ops.example.com"
os.environ["SESSION_SECRET"] = "test-secret"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    from app.config import reset_config
    from app.db import reset_engine
    reset_config()
    reset_engine()
    from app.cli import migrate
    migrate()
    yield tmp_path
    reset_engine()
    reset_config()


@pytest.fixture()
def db(env):
    from app.db import SessionLocal
    s = SessionLocal()
    yield s
    s.close()


@pytest.fixture()
def users(db):
    from app.services.users import create_user
    admin = create_user(db, "100000000000000001", "管理者A", 2)
    lv2b = create_user(db, "100000000000000002", "管理者B", 2)
    staff = create_user(db, "100000000000000003", "スタッフ", 1)
    db.commit()
    return {"admin": admin, "lv2b": lv2b, "staff": staff}


@pytest.fixture()
def app(env):
    from app.web.main import create_app
    return create_app()


def make_client(app, discord_id: str | None):
    from fastapi.testclient import TestClient
    c = TestClient(app, base_url="https://testserver", follow_redirects=False)
    if discord_id:
        r = c.post("/auth/dev-login", data={"discord_user_id": discord_id})
        assert r.status_code == 303, r.text
    return c


@pytest.fixture()
def admin_client(app, users):
    return make_client(app, users["admin"].discord_user_id)


@pytest.fixture()
def staff_client(app, users):
    return make_client(app, users["staff"].discord_user_id)


def jpeg_bytes(w=1200, h=2000, color=(200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "JPEG")
    return buf.getvalue()
