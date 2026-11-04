"""権限のない操作をAPIを直接叩いて試しても拒否される。"""
from __future__ import annotations

import pytest
from sqlalchemy import text

LV2_ONLY = [
    ("post", "/pricing/1/approve", {"start": "1"}),
    ("post", "/drafts/1/confirm", {}),
    ("post", "/items/1/withdraw", {"reason": "x"}),
    ("post", "/items/1/rollback", {"to_status": "INTAKE", "reason": "x"}),
    ("post", "/orders/1/cancel", {"reason": "x"}),
    ("post", "/orders/1/status", {"to_status": "COMPLETED", "reason": "x"}),
    ("post", "/orders/1/edit", {"reason": "x"}),
    ("post", "/orders/1/tracking/delete", {"reason": "x"}),
    ("post", "/orders/1/case-close", {"note": "x"}),
    ("post", "/settings/json/fees", {"value": "{}"}),
    ("post", "/settings/general", {}),
    ("post", "/settings/duty", {}),
    ("post", "/settings/override", {"d": "2026-11-05"}),
    ("post", "/settings/template/title", {"body": "x"}),
    ("post", "/users", {"discord_user_id": "100000000000000009", "display_name": "x"}),
    ("post", "/users/1/level", {"level": "1"}),
    ("post", "/users/1/deactivate", {}),
    ("get", "/settings", None),
    ("get", "/users", None),
]


@pytest.mark.parametrize("method,path,data", LV2_ONLY)
def test_lv1_rejected(staff_client, method, path, data):
    r = getattr(staff_client, method)(path, data=data) if data is not None else getattr(staff_client, method)(path)
    assert r.status_code == 403, (path, r.status_code)


def test_unauthenticated(app, users):
    from tests.conftest import make_client
    c = make_client(app, None)
    assert c.get("/").status_code == 303
    assert c.get("/api/lookup?cert=1").status_code == 401
    assert c.post("/intake", data={}).status_code == 401


def test_unknown_discord_user_cannot_login(app, users):
    from tests.conftest import make_client
    c = make_client(app, None)
    r = c.post("/auth/dev-login", data={"discord_user_id": "999999999999999999"})
    assert r.status_code == 403


def test_user_safeguards(db, users):
    from app.services import users as user_svc
    from app.services.errors import PermissionDenied, ServiceError
    admin, lv2b = users["admin"], users["lv2b"]
    with pytest.raises(PermissionDenied):
        user_svc.change_level(db, admin, admin, 1)
    user_svc.change_level(db, admin, lv2b, 1)
    with pytest.raises(ServiceError):
        # 最後のLv2は降格・削除できない（自分以外の操作者が必要になる状況を作る）
        user_svc.change_level(db, lv2b.__class__(id=-1, level=2, display_name="ghost"), admin, 1)
    with pytest.raises(ServiceError):
        user_svc.deactivate(db, lv2b.__class__(id=-1, level=2, display_name="ghost"), admin)
    db.commit()
    from app.models import AuditLog, Outbox
    assert db.query(AuditLog).filter(AuditLog.action == "user.level").count() == 1
    assert any(o.payload.get("channel") == "system" for o in db.query(Outbox).all())


def test_audit_log_append_only(db, users):
    from sqlalchemy.exc import IntegrityError, OperationalError
    with pytest.raises((IntegrityError, OperationalError)):
        db.execute(text("UPDATE audit_logs SET action='x'"))
    db.rollback()
    with pytest.raises((IntegrityError, OperationalError)):
        db.execute(text("DELETE FROM audit_logs"))
    db.rollback()


def test_purchase_price_hidden_from_other_lv1(app, admin_client, users, db):
    from tests.conftest import make_client
    from tests.test_flow import done_id, register
    r = register(admin_client, cert="55556666", purchase_price_jpy="12345")
    item_id = done_id(r)
    staff = make_client(app, users["staff"].discord_user_id)
    page = staff.get(f"/items/{item_id}")
    assert "12345" not in page.text
    assert "12345" in admin_client.get(f"/items/{item_id}").text
    # Lv1 が仕入値を書き換えようとしても無視される／拒否される
    from app.services import items as item_svc
    from app.services.errors import PermissionDenied
    it = item_svc.get_item(db, item_id)
    with pytest.raises(PermissionDenied):
        item_svc.update_item(db, users["staff"], it, {"purchase_price_jpy": "1"}, None)


def test_optimistic_lock(admin_client, db, users):
    from tests.test_flow import done_id, register
    r = register(admin_client, cert="77778888")
    item_id = done_id(r)
    r1 = admin_client.post(f"/items/{item_id}/edit", data={"version": "1", "memo": "A"})
    assert r1.status_code == 303
    r2 = admin_client.post(f"/items/{item_id}/edit", data={"version": "1", "memo": "B"},
                           headers={"referer": f"https://testserver/items/{item_id}"})
    assert r2.status_code == 303
    page = admin_client.get(f"/items/{item_id}")
    assert "他のユーザーが先に更新しました" in page.text
    assert 'value="A"' in page.text
