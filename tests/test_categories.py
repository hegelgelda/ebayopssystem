"""PSA以外の区分（シングル・パック・BOX・バルク）と、通常出品／オークションのレーン分け。"""
from __future__ import annotations

import csv
import io

from sqlalchemy import select

from tests.test_flow import done_id, run_jobs, upload


def _cards(db, env):
    from app.services import importers
    p = env / "cards.csv"
    p.write_text("model_id,set_name,name_jp,name_en\n"
                 "SV2a-201,Pokemon Card 151,リザードンex,Charizard ex\n"
                 "SV4a-201,Shiny Treasure ex,ピカチュウ,Pikachu\n"
                 "SV2a-025,Pokemon Card 151,ピカチュウ,Pikachu\n", encoding="utf-8")
    importers.import_cards(db, p)
    db.commit()


def intake(client, **kw):
    data = {"purchase_price_jpy": "1000", "storage_box": "C1", "purchase_date": "2026-11-01"}
    data.update(kw)
    return client.post("/intake", data=data)


def ready_item(admin, item_id, start="20", bin=""):
    """撮影 → 承認 → 下書き → 確認 で READY にし、下書きIDを返す。"""
    upload(admin, item_id, "front", n=item_id)
    upload(admin, item_id, "back", n=item_id)
    run_jobs()
    r = admin.post(f"/pricing/{item_id}/approve", data={"start": start, "bin": bin})
    assert r.status_code == 303, r.text
    r = admin.post("/listings/generate", data={"item_id": str(item_id)})
    draft_id = int(r.headers["location"].rsplit("/", 1)[1])
    assert admin.post(f"/drafts/{draft_id}/confirm").status_code == 303
    return draft_id


def test_number_search(admin_client, db, env):
    _cards(db, env)
    js = admin_client.get("/api/catalog/search?number=201/165").json()
    assert js["number"] == "201" and {c["model_id"] for c in js["candidates"]} == {"SV2a-201", "SV4a-201"}
    js = admin_client.get("/api/catalog/search?number=201&set=151").json()
    assert [c["model_id"] for c in js["candidates"]] == ["SV2a-201"]
    js = admin_client.get("/api/catalog/search?number=No.025").json()
    assert [c["model_id"] for c in js["candidates"]] == ["SV2a-025"]
    sets = admin_client.get("/api/catalog/sets?q=151").json()["sets"]
    assert sets == [{"set_id": "SV2a", "set_name": "Pokemon Card 151"}]


def test_single_fixed_multi_quantity_flow(admin_client, db, env, users):
    from app.db import SessionLocal
    from app.models import Item, Listing, Order
    _cards(db, env)
    # シングル：型番で選んだ値＋状態。既定レーンは通常出品
    r = intake(admin_client, category="single", model_id="SV2a-201", card_name_en="Charizard ex",
               set_name="Pokemon Card 151", condition="nm", quantity="3")
    assert r.status_code == 303, r.text
    item_id = done_id(r)
    with SessionLocal() as s:
        it = s.get(Item, item_id)
        assert it.sku == f"S-{item_id:05d}" and it.cert_number is None
        assert it.listing_format == "fixed" and it.quantity == 3 and it.set_id == "SV2a" and it.card_number == "201"

    # 撮影はSKUのフォルダに置かれ、公開URLにもSKUが入る
    upload(admin_client, item_id, "front", n=1)
    r = upload(admin_client, item_id, "back", n=1)
    assert r.json()["item_status"] == "PHOTOGRAPHED"
    run_jobs()
    js = admin_client.get(f"/api/items/{item_id}/photos").json()
    assert f"/img/S-{item_id:05d}/1-" in js["photos"][0]["public_url"]

    # 価格承認：通常出品は販売価格だけ
    page = admin_client.get(f"/pricing/{item_id}").text
    assert "販売価格" in page and "即決価格（USD・任意）" not in page
    admin_client.post(f"/pricing/{item_id}/approve", data={"start": "25", "bin": "999"})
    r = admin_client.post("/listings/generate", data={"item_id": str(item_id)})
    draft_id = int(r.headers["location"].rsplit("/", 1)[1])
    admin_client.post(f"/drafts/{draft_id}/confirm")

    r = admin_client.post("/listings/export", data={"draft_id": str(draft_id)})
    assert "lane=fixed" in r.headers["location"]
    batch = r.headers["location"].split("batch=")[1]
    rows = list(csv.reader(io.StringIO(admin_client.get(f"/exports/{batch}.csv").content.decode("utf-8-sig"))))
    rec = dict(zip(rows[0], rows[1]))
    assert rec["CustomLabel"] == f"S-{item_id:05d}"
    assert rec["*Format"] == "FixedPrice" and rec["*Duration"] == "GTC" and rec["*Quantity"] == "3"
    assert rec["*StartPrice"] == "25.00" and rec["BuyItNowPrice"] == ""
    assert rec["*ConditionID"] == "4000" and rec["CD:Card Condition - (ID: 40001)"] == "400010"
    assert rec["CD:Professional Grader - (ID: 27501)"] == "" and rec["CDA:Certification Number - (ID: 27503)"] == ""
    assert rec["C:Graded"] == "No" and "NM" in rec["*Title"]

    result = f"Status,Item ID,Custom Label\nSuccess,123456789012,S-{item_id:05d}\n"
    admin_client.post("/listings/import", files={"file": ("r.csv", result.encode(), "text/csv")})
    with SessionLocal() as s:
        lst = s.scalar(select(Listing).where(Listing.item_id == item_id))
        assert lst.status == "active" and lst.quantity == 3 and lst.end_time is None
        lid = lst.id

    # 2個売れる → 出品は続く（残り1）
    r = admin_client.post(f"/listings/{lid}/sale", data={"buyer": "b1", "amount": "50", "ship_country": "US",
                                                       "quantity": "2", "paid": "1"})
    o1 = int(r.headers["location"].rsplit("/", 1)[1])
    with SessionLocal() as s:
        it = s.get(Item, item_id)
        assert it.status == "LISTED" and it.remaining == 1 and s.get(Listing, lid).status == "active"
        assert s.get(Order, o1).quantity == 2 and s.get(Order, o1).status == "AWAITING_SHIPMENT"
    # 残りより多くは売れない
    r = admin_client.post(f"/listings/{lid}/sale", data={"buyer": "b2", "amount": "25", "ship_country": "US",
                                                       "quantity": "2"}, headers={"accept": "application/json"})
    assert r.status_code == 400
    # 最後の1個 → 売り切れ
    r = admin_client.post(f"/listings/{lid}/sale", data={"buyer": "b2", "amount": "25", "ship_country": "GB"})
    o2 = int(r.headers["location"].rsplit("/", 1)[1])
    with SessionLocal() as s:
        assert s.get(Item, item_id).status == "SOLD" and s.get(Listing, lid).status == "sold"
    # 未払いキャンセル → 在庫が1個戻り、価格確定へ
    admin_client.post(f"/orders/{o2}/cancel", data={"reason": "未払い"})
    with SessionLocal() as s:
        it = s.get(Item, item_id)
        assert it.status == "PRICED" and it.remaining == 1
    # 粗利は仕入値×数量
    from app.services import orders as order_svc
    with SessionLocal() as s:
        gp = order_svc.gross_profit(s, s.get(Order, o1))
        assert gp["purchase_jpy"] == 2000


def test_lanes_do_not_mix(admin_client, db, env, users):
    from tests.test_flow import register
    r = register(admin_client, cert="11112222")  # PSA → 既定はオークション
    psa_id = done_id(r)
    r = intake(admin_client, category="box", card_name_en="Pokemon Card 151 Booster Box", set_id="SV2a",
               set_name="Pokemon Card 151")
    box_id = done_id(r)
    d_auction = ready_item(admin_client, psa_id, start="200", bin="300")
    d_fixed = ready_item(admin_client, box_id, start="150")

    fixed_page = admin_client.get("/listings?lane=fixed").text
    auction_page = admin_client.get("/listings?lane=auction").text
    assert "Booster Box" in fixed_page and "Charizard" not in fixed_page
    assert "Charizard" in auction_page and "Booster Box" not in auction_page
    # 混ぜてCSVを出そうとしても拒否される
    r = admin_client.post("/listings/export", data={"draft_id": [str(d_auction), str(d_fixed)]},
                          headers={"accept": "application/json"})
    assert r.status_code == 400 and "別々" in r.json()["error"]
    # 承認待ちもレーンごと
    r = intake(admin_client, category="pack", card_name_en="151 Pack", quantity="10")
    pack_id = done_id(r)
    upload(admin_client, pack_id, "front", n=pack_id)
    upload(admin_client, pack_id, "back", n=pack_id)
    r = admin_client.get("/pricing?lane=auction")
    assert r.status_code == 200  # オークションの承認待ちは無い
    assert admin_client.get("/pricing?lane=fixed").headers["location"] == f"/pricing/{pack_id}"


def test_category_rules(admin_client, db, env, users):
    from app.db import SessionLocal
    from app.models import Item
    # シングルは状態とモデルIDが必須
    assert intake(admin_client, category="single", model_id="SV2a-201").status_code == 400
    assert intake(admin_client, category="single", condition="nm").status_code == 400
    # 数量2以上はオークション不可
    r = intake(admin_client, category="pack", card_name_en="Pack", quantity="5", listing_format="auction")
    assert r.status_code == 400
    # バルクは全体写真1枚で撮影済み、数量は常に1
    r = intake(admin_client, category="bulk", card_name_jp="ノーマル100枚", memo="100 cards", quantity="9")
    bulk_id = done_id(r)
    r = upload(admin_client, bulk_id, "front", n=99)
    assert r.json()["item_status"] == "PHOTOGRAPHED"
    with SessionLocal() as s:
        it = s.get(Item, bulk_id)
        assert it.quantity == 1 and it.sku == f"L-{bulk_id:05d}" and it.listing_format == "auction"
    # BOX はモデルIDなしで登録できる
    r = intake(admin_client, category="box", set_name="Shiny Treasure ex")
    with SessionLocal() as s:
        assert s.get(Item, done_id(r)).sku.startswith("X-")
    # 撮影画面は SKU でも開ける
    assert admin_client.get(f"/photos/scan?cert=l-{bulk_id:05d}").headers["location"] == f"/photos/{bulk_id}"


def test_switch_lane_requires_reapproval(admin_client, db, env, users):
    from app.db import SessionLocal
    from app.models import Item
    r = intake(admin_client, category="single", model_id="SV2a-201", condition="ex", card_name_en="Charizard ex")
    item_id = done_id(r)
    upload(admin_client, item_id, "front", n=7)
    upload(admin_client, item_id, "back", n=7)
    run_jobs()
    admin_client.post(f"/pricing/{item_id}/approve", data={"start": "30"})
    with SessionLocal() as s:
        it = s.get(Item, item_id)
        assert it.status == "PRICED"
        v = it.version
    # オークションへ移す → 新しい価格案が承認待ちになり、承認するまで下書きを作れない
    r = admin_client.post(f"/items/{item_id}/format", data={"listing_format": "auction", "version": str(v)})
    assert r.status_code == 303
    assert admin_client.post("/listings/generate", data={"item_id": str(item_id)},
                             headers={"accept": "application/json"}).status_code == 400
    assert admin_client.get("/pricing?lane=auction").headers["location"] == f"/pricing/{item_id}"
    admin_client.post(f"/pricing/{item_id}/approve", data={"start": "20", "bin": "40"})
    r = admin_client.post("/listings/generate", data={"item_id": str(item_id)})
    draft_id = int(r.headers["location"].rsplit("/", 1)[1])
    from app.models import ListingDraft
    with SessionLocal() as s:
        d = s.get(ListingDraft, draft_id)
        assert d.format == "auction" and d.quantity == 1 and str(d.bin_price) == "40.00"
