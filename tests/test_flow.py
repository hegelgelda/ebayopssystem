"""Phase 1 の受入基準に沿った通し試験（manual アダプタ）。"""
from __future__ import annotations

import csv
import io
from datetime import timedelta

from sqlalchemy import select

from tests.conftest import jpeg_bytes


def done_id(r) -> int:
    from urllib.parse import parse_qs, urlparse
    return int(parse_qs(urlparse(r.headers["location"]).query)["done"][0])


def register(client, cert="12345678", **kw):
    data = {"cert_number": cert, "model_id": "SV2a-201", "card_name_jp": "リザードンex", "card_name_en": "Charizard ex",
            "set_name": "Pokemon Card 151", "grade": "10", "grader": "PSA", "purchase_price_jpy": "30000",
            "purchase_date": "2026-11-01", "storage_box": "B1", "storage_slot": "3"}
    data.update(kw)
    return client.post("/intake", data=data)


def upload(client, item_id, kind, n=0):
    return client.post(f"/api/items/{item_id}/photos", data={"kind": kind, "client_upload_id": f"{kind}-{item_id}-{n}"},
                       files={"file": (f"{kind}.jpg", jpeg_bytes(), "image/jpeg")})


def run_jobs():
    from app.db import SessionLocal
    from app.services import photos
    with SessionLocal() as s:
        photos.run_pending_jobs(s)


def item_by_cert(cert):
    from app.db import SessionLocal
    from app.models import Item
    s = SessionLocal()
    return s, s.scalar(select(Item).where(Item.cert_number == cert))


def test_full_flow(admin_client, staff_client, users, env):
    from app.db import SessionLocal
    from app.models import AuditLog, DutySchedule, ItemStatus, Notification, Order, Outbox
    from app.config import get_config

    # 今日の曜日担当をスタッフに
    with SessionLocal() as s:
        for wd in range(7):
            s.merge(DutySchedule(weekday=wd, user_id=users["staff"].id))
        s.commit()

    # 入荷登録（Lv1）
    r = register(staff_client)
    assert r.status_code == 303 and r.headers["location"].startswith("/intake?done=")
    item_id = done_id(r)
    # 重複登録は拒否され、既存レコードへ
    r = register(staff_client)
    assert r.status_code == 303 and r.headers["location"] == f"/items/{item_id}"

    # 撮影：表裏で PHOTOGRAPHED
    assert upload(staff_client, item_id, "front").status_code == 200
    r = upload(staff_client, item_id, "back")
    assert r.json()["item_status"] == ItemStatus.PHOTOGRAPHED
    # 同じ client_upload_id の再送は重複しない
    upload(staff_client, item_id, "back")
    run_jobs()
    js = staff_client.get(f"/api/items/{item_id}/photos").json()
    assert len(js["photos"]) == 2 and all(p["status"] == "done" for p in js["photos"])
    pub = js["photos"][0]["public_url"]
    assert pub.startswith("https://ops.example.com/img/12345678/1-")
    # 公開URL：ログインなしで閲覧でき、長辺1600px
    from fastapi.testclient import TestClient
    anon = TestClient(admin_client.app, base_url="https://testserver")
    img = anon.get(pub.replace("https://ops.example.com", ""))
    assert img.status_code == 200
    from PIL import Image
    im = Image.open(io.BytesIO(img.content))
    assert max(im.size) == 1600
    assert (get_config().public_images_dir / "12345678").exists()

    # 価格：Lv1は提案のみ、承認は拒否
    r = staff_client.post(f"/pricing/{item_id}/propose", data={"start": "250", "bin": "400", "format": "auction"})
    assert r.status_code == 303
    r = staff_client.post(f"/pricing/{item_id}/approve", data={"start": "250", "bin": "400"})
    assert r.status_code == 403
    page = admin_client.get(f"/pricing/{item_id}")
    assert page.status_code == 200 and "損益分岐価格" in page.text
    r = admin_client.post(f"/pricing/{item_id}/approve", data={"start": "260", "bin": "420", "format": "auction"})
    assert r.status_code == 303
    s, item = item_by_cert("12345678")
    assert item.status == ItemStatus.PRICED
    s.close()

    # 出品データ生成 → Lv2 が確認 → READY
    r = staff_client.post("/listings/generate", data={"item_id": str(item_id)})
    draft_id = int(r.headers["location"].rsplit("/", 1)[1])
    assert staff_client.post(f"/drafts/{draft_id}/confirm").status_code == 403
    assert admin_client.get(f"/drafts/{draft_id}").status_code == 200
    r = admin_client.post(f"/drafts/{draft_id}/confirm")
    assert r.status_code == 303
    s, item = item_by_cert("12345678")
    assert item.status == ItemStatus.READY
    s.close()

    # CSV出力
    r = staff_client.post("/listings/export", data={"draft_id": str(draft_id)})
    batch = r.headers["location"].split("batch=")[1]
    csv_text = staff_client.get(f"/exports/{batch}.csv").content.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(csv_text)))
    header, row = rows[0], rows[1]
    rec = dict(zip(header, row))
    assert rec["CustomLabel"] == "12345678"
    assert len(rec["*Title"]) <= 80 and "Charizard ex" in rec["*Title"]
    assert rec["*StartPrice"] == "260.00" and rec["BuyItNowPrice"] == "420.00"
    assert rec["PicURL"].count("https://ops.example.com/img/") == 2
    assert rec["C:Certification Number"] == "12345678"
    assert rec["*Format"] == "Auction" and rec["*Duration"] == "7"

    # 結果CSVを取り込む → LISTED
    result = "Line Number,Action,Status,Item ID,Custom Label,Error Message\n1,Add,Success,123456789012,12345678,\n"
    r = staff_client.post("/listings/import", files={"file": ("result.csv", result.encode(), "text/csv")})
    assert r.status_code == 303
    s, item = item_by_cert("12345678")
    assert item.status == ItemStatus.LISTED
    from app.models import Listing
    lst = s.scalar(select(Listing).where(Listing.item_id == item.id))
    assert lst.ebay_item_id == "123456789012" and lst.status == "active"
    s.close()

    # 落札を記録 → #orders スレッド投稿＋担当メンション
    r = staff_client.post(f"/listings/{lst.id}/sale", data={"buyer": "buyer_us", "amount": "300", "currency": "USD",
                                                          "ship_country": "us"})
    assert r.status_code == 303
    order_id = int(r.headers["location"].rsplit("/", 1)[1])
    with SessionLocal() as s:
        o = s.get(Order, order_id)
        assert o.status == "AWAITING_PAYMENT" and o.ship_country == "US"
        assert o.payment_due - o.sold_at == timedelta(days=4)
        obs = s.scalars(select(Outbox).where(Outbox.kind == "post")).all()
        posted = [ob for ob in obs if ob.payload.get("order_id") == order_id]
        assert posted and posted[0].payload["channel"] == "orders"
        assert users["staff"].discord_user_id in posted[0].payload["mention_user_ids"]
        n = s.scalar(select(Notification).where(Notification.event_type == "order_sold"))
        assert n.escalate_at is not None
        assert s.get(type(item), item.id).status == ItemStatus.SOLD

    # 支払 → 発送期限
    r = staff_client.post(f"/orders/{order_id}/payment", data={})
    with SessionLocal() as s:
        o = s.get(Order, order_id)
        assert o.status == "AWAITING_SHIPMENT" and o.ship_by is not None

    # 追跡番号：形式チェック
    r = staff_client.post(f"/orders/{order_id}/tracking/confirm", data={"carrier": "FEDEX", "number": "123"},
                          headers={"referer": f"https://testserver/orders/{order_id}"})
    assert r.status_code == 303  # エラーで戻る
    r = staff_client.post(f"/orders/{order_id}/tracking/confirm", data={"carrier": "FEDEX", "number": "1234 5678 9012"})
    assert r.status_code == 200 and "123456789012" in r.text
    r = staff_client.post(f"/orders/{order_id}/tracking", data={"carrier": "FEDEX", "number": "123456789012"})
    assert r.status_code == 303
    page = staff_client.get(f"/orders/{order_id}")
    assert "Seller Hub で追跡番号を登録する手順" in page.text
    # Lv1 は修正・削除できない
    assert staff_client.post(f"/orders/{order_id}/tracking", data={"carrier": "DHL", "number": "1234567890"},
                             headers={"accept": "application/json"}).status_code == 403
    assert staff_client.post(f"/orders/{order_id}/tracking/delete", data={"reason": "x"}).status_code == 403
    staff_client.post(f"/orders/{order_id}/tracking/uploaded")
    with SessionLocal() as s:
        o = s.get(Order, order_id)
        assert o.status == "SHIPPED" and o.active_shipment().ebay_upload_status == "uploaded"

    # 配達完了 → 48時間で COMPLETED（粗利確定）
    staff_client.post(f"/orders/{order_id}/tracking-event", data={"status": "delivered"})
    from app.services import orders as order_svc
    from app.timeutil import utcnow
    with SessionLocal() as s:
        order_svc.run_deadline_checks(s, utcnow() + timedelta(hours=49))
        s.commit()
        o = s.get(Order, order_id)
        assert o.status == "COMPLETED" and o.gross_profit_jpy is not None

    # 全操作が操作ログに残る
    with SessionLocal() as s:
        actions = {a for (a,) in s.execute(select(AuditLog.action))}
    for a in ["item.create", "photo.upload", "item.photographed", "price.propose", "price.approve", "draft.generate",
              "draft.confirm", "listing.export_csv", "listing.import_result", "item.listed", "order.create",
              "order.payment_recorded", "shipment.create", "shipment.ebay_uploaded", "tracking.manual_event",
              "order.completed", "notify.sent"]:
        assert a in actions, a


def test_unsold_relist_suggests_discount(admin_client, users, env):
    from app.db import SessionLocal
    from app.models import ItemStatus, ListingDraft
    from app.adapters.manual import mark_listed
    from app.services import pricing
    r = register(admin_client, cert="22223333")
    item_id = done_id(r)
    upload(admin_client, item_id, "front")
    upload(admin_client, item_id, "back")
    run_jobs()
    admin_client.post(f"/pricing/{item_id}/approve", data={"start": "200", "bin": "300", "format": "auction"})
    r = admin_client.post("/listings/generate", data={"item_id": str(item_id)})
    draft_id = int(r.headers["location"].rsplit("/", 1)[1])
    admin_client.post(f"/drafts/{draft_id}/confirm")
    with SessionLocal() as s:
        d = s.get(ListingDraft, draft_id)
        lst = mark_listed(s, s.get(type(users["admin"]), users["admin"].id), d, "998877665544")
        s.commit()
        lid = lst.id
    admin_client.post(f"/listings/{lid}/unsold")
    with SessionLocal() as s:
        from app.models import Item
        it = s.get(Item, item_id)
        assert it.status == ItemStatus.PRICED and it.relist_count == 1
        q = pricing.open_quote(s, it)
        assert q.suggested_start == 190 and q.suggested_bin == 285
    # 再出品の承認前は下書きを作れない
    r = admin_client.post("/listings/generate", data={"item_id": str(item_id)},
                          headers={"accept": "application/json"})
    assert r.status_code == 400


def test_duplicate_active_listing_blocked(db, users):
    from app.models import Item, Listing
    from sqlalchemy.exc import IntegrityError
    import pytest
    it = Item(cert_number="9", model_id="X-1", status="LISTED")
    db.add(it)
    db.flush()
    db.add(Listing(item_id=it.id, title="a", format="auction", status="active"))
    db.flush()
    db.add(Listing(item_id=it.id, title="b", format="auction", status="active"))
    with pytest.raises(IntegrityError):
        db.flush()


def test_all_pages_render(admin_client, staff_client, users, env):
    test_full_flow(admin_client, staff_client, users, env)
    pages = ["/", "/items", "/items?status=SOLD&q=Char", "/intake", "/intake?cert=12345678", "/photos", "/photos/1",
             "/pricing", "/pricing/1", "/listings", "/drafts/1", "/orders", "/orders?status=open", "/orders/1",
             "/items/1", "/settings", "/users", "/audit", "/audit?target_type=order", "/login",
             "/manifest.webmanifest", "/sw.js", "/healthz"]
    for p in pages:
        r = admin_client.get(p)
        assert r.status_code == 200, (p, r.status_code, r.text[:500])
    for p in ["/", "/items/1", "/orders/1", "/audit", "/pricing/1"]:
        r = staff_client.get(p)
        assert r.status_code == 200, p
