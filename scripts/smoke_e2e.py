"""起動中の docker compose 環境に対する通し確認（manual アダプタ）。

APP_ENV=dev / DEV_LOGIN=1 で起動し、create-admin で Lv2(111111111111111111) と Lv1(222222222222222222) を作ってから:
  python scripts/smoke_e2e.py https://localhost:8443
"""
from __future__ import annotations

import csv
import io
import random
import sys
import time

import httpx
from PIL import Image, ImageDraw

BASE = sys.argv[1] if len(sys.argv) > 1 else "https://localhost:8443"
LV2, LV1 = "111111111111111111", "222222222222222222"


def client(discord_id: str | None) -> httpx.Client:
    c = httpx.Client(base_url=BASE, verify=False, follow_redirects=False, timeout=60)
    if discord_id:
        r = c.post("/auth/dev-login", data={"discord_user_id": discord_id})
        assert r.status_code == 303, r.text
    return c


def card_photo(color) -> bytes:
    im = Image.new("RGB", (3000, 4000), (235, 235, 235))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([700, 600, 2300, 3400], radius=60, fill=(250, 250, 250), outline=(120, 120, 120), width=12)
    d.rectangle([800, 700, 2200, 1000], fill=(200, 30, 30))
    d.rectangle([850, 1100, 2150, 3200], fill=color)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=92)
    return buf.getvalue()


def step(msg):
    print(f"✔ {msg}", flush=True)


def main():
    a, s = client(LV2), client(LV1)
    cert = str(random.randint(10_000_000, 99_999_999))
    r = s.post("/intake", data={"cert_number": cert, "model_id": "SV2a-201", "card_name_jp": "リザードンex",
                                "card_name_en": "Charizard ex", "set_name": "Pokemon Card 151", "grade": "10",
                                "purchase_price_jpy": "30000", "storage_box": "B1", "storage_slot": "1"})
    assert r.status_code == 303 and "done=" in r.headers["location"], (r.status_code, r.text[:300])
    item_id = int(r.headers["location"].split("=")[1])
    step(f"入荷登録 cert {cert} → item {item_id}")

    for kind, color in (("front", (40, 90, 200)), ("back", (30, 140, 60))):
        r = s.post(f"/api/items/{item_id}/photos", data={"kind": kind, "client_upload_id": f"{cert}-{kind}"},
                   files={"file": (f"{kind}.jpg", card_photo(color), "image/jpeg")})
        assert r.status_code == 200, r.text
    assert r.json()["item_status"] == "PHOTOGRAPHED"
    step("表裏アップロード → PHOTOGRAPHED")

    t0 = time.time()
    while True:
        js = s.get(f"/api/items/{item_id}/photos").json()
        if all(p["status"] != "pending" for p in js["photos"]):
            break
        if time.time() - t0 > 600:
            raise SystemExit("画像整形がタイムアウト")
        time.sleep(2)
    assert all(p["status"] == "done" for p in js["photos"]), js
    step(f"worker で整形完了（{time.time() - t0:.0f}秒、背景除去={[p['bg_removed'] for p in js['photos']]}）")

    anon = client(None)
    for p in js["photos"]:
        path = p["public_url"].split("://", 1)[1].split("/", 1)[1]
        r = anon.get("/" + path)
        assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg", (path, r.status_code)
        im = Image.open(io.BytesIO(r.content))
        assert max(im.size) == 1600, im.size
    assert anon.get(f"/img/{cert}/").status_code == 404
    assert anon.get("/").status_code == 303
    step("公開URL（Caddy /img/）はログインなしで取得でき長辺1600px、ディレクトリ一覧は不可、管理画面はログイン必須")

    assert s.post(f"/pricing/{item_id}/approve", data={"start": "250"}).status_code == 403
    page = a.get(f"/pricing/{item_id}")
    assert "損益分岐価格" in page.text and "USD/JPY" in page.text
    r = a.post(f"/pricing/{item_id}/approve", data={"start": "260", "bin": "420", "format": "auction"})
    assert r.status_code == 303
    step("Lv1 の承認は 403、Lv2 の承認で PRICED")

    r = s.post("/listings/generate", data={"item_id": str(item_id)})
    draft_id = int(r.headers["location"].rsplit("/", 1)[1])
    r = a.post(f"/drafts/{draft_id}/confirm")
    assert r.status_code == 303 and r.headers["location"] == "/listings", r.headers
    r = s.post("/listings/export", data={"draft_id": str(draft_id)})
    batch = r.headers["location"].split("batch=")[1]
    text = s.get(f"/exports/{batch}.csv").content.decode("utf-8-sig")
    rec = dict(zip(*list(csv.reader(io.StringIO(text)))[:2]))
    assert rec["CustomLabel"] == cert and rec["PicURL"].count("/img/") == 2
    step(f"出品データ → READY → CSV出力（{batch}、タイトル「{rec['*Title']}」）")

    ebay_id = str(random.randint(100_000_000_000, 999_999_999_999))
    result = f"Line Number,Status,Item ID,Custom Label,Error Message\n1,Success,{ebay_id},{cert},\n"
    r = s.post("/listings/import", files={"file": ("result.csv", result.encode(), "text/csv")})
    assert r.status_code == 303
    assert "出品中" in s.get(f"/items/{item_id}").text
    step(f"結果CSVの取り込み → LISTED（eBay {ebay_id}）")

    lid = int(s.get(f"/items/{item_id}").text.split('/listings/')[1].split('/')[0])
    r = s.post(f"/listings/{lid}/sale", data={"buyer": "smoke_buyer", "amount": "310", "currency": "USD",
                                             "ship_country": "US"})
    order_id = int(r.headers["location"].rsplit("/", 1)[1])
    s.post(f"/orders/{order_id}/payment", data={})
    r = s.post(f"/orders/{order_id}/tracking", data={"carrier": "DHL", "number": "1234567890"})
    assert r.status_code == 303
    page = s.get(f"/orders/{order_id}").text
    assert "Seller Hub で追跡番号を登録する手順" in page and "発送済み" in page
    s.post(f"/orders/{order_id}/tracking/uploaded")
    step(f"落札記録 → 支払 → 追跡番号（Seller Hub 手順表示）→ eBay登録済み（注文 {order_id}）")

    assert "order.create" in a.get(f"/audit?target_type=order&target_id={order_id}").text
    step("操作ログに記録")
    print("ALL OK")


if __name__ == "__main__":
    main()
