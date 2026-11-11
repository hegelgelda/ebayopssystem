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
    assert sets == [{"set_id": "SV2a", "set_name": "Pokemon Card 151", "set_name_jp": None}]


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
    rows = list(csv.reader(io.StringIO(admin_client.get(f"/exports/{batch}.csv").content.decode("utf-8-sig"))))[1:]
    rec = dict(zip(rows[0], rows[1]))
    assert rec["CustomLabel"] == f"S-{item_id:05d}"
    assert rec["*Format"] == "FixedPrice" and rec["*Duration"] == "GTC" and rec["*Quantity"] == "3"
    assert rec["*StartPrice"] == "25.00" and rec["BuyItNowPrice"] == ""
    assert rec["*ConditionID"] == "4000" and rec["CD:Card Condition - (ID: 40001)"] == "400010"
    assert rec["CD:Professional Grader - (ID: 27501)"] == "" and rec["CDA:Certification Number - (ID: 27503)"] == ""
    assert "C:Graded" not in rec and "NM" in rec["*Title"]
    # 状態 LP は 400015（eBay テンプレートの値）
    from app.settings_store import get_setting
    assert {c["key"]: c["ebay_id"] for c in get_setting(db, "raw_conditions")}["ex"] == "400015"

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
    r = intake(admin_client, category="pack", card_name_en="151 Pack", set_name="Pokemon Card 151", quantity="10")
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
    r = intake(admin_client, category="bulk", card_name_jp="ノーマル100枚", memo="100 cards", quantity="9", card_count="100")
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


TCG_HTML = """<html><script>
const SERIES   = [{"key": "Scarlet & Violet", "sets": [{"id": "SV2a", "name": "Pok\u00e9mon Card 151", "jp": "\u30dd\u30b1\u30e2\u30f3\u30ab\u30fc\u30c9151", "count": 2}]},
                  {"key": "Promo", "sets": [{"id": "SV-P", "name": "SV promo", "jp": "SV\u30d7\u30ed\u30e2", "count": 1}]}];
const CARDS    = {"SV2a": [{"n": "201", "en": "Charizard ex", "jp": "\u30ea\u30b6\u30fc\u30c9\u30f3ex", "r": "Rare"},
                           {"n": "25", "en": "Pikachu", "jp": "\u30d4\u30ab\u30c1\u30e5\u30a6", "r": "Unknown"}],
                  "SV-P": [{"n": "001", "en": "\u30d4\u30ab\u30c1\u30e5\u30a6", "jp": "\u30d4\u30ab\u30c1\u30e5\u30a6", "r": "Promo"}]};
const PROMO_SETS = new Set(["SV-P"]);
const BOXES    = [];
</script></html>"""


def test_tcg_catalog_import(db, env, admin_client, monkeypatch):
    from app.models import CardCatalog
    from app.services import importers
    rows = importers.parse_tcg_catalog(TCG_HTML, "https://example.github.io/tcg-catalog/")
    assert len(rows) == 3
    by = {r["model_id"]: r for r in rows}
    assert by["SV2a-201"]["name_en"] == "Charizard ex" and by["SV2a-201"]["set_name_jp"] == "ポケモンカード151"
    assert by["SV2a-201"]["image_url"].endswith("/tpc/SV2a/SV2a_201_R_JP_LG.png")
    assert by["SV2a-25"]["rarity"] is None
    # プロモは en に日本語が入っているので英語名にしない。画像はカタログ側のパス
    assert by["SV-P-001"]["name_en"] is None
    assert by["SV-P-001"]["image_url"] == "https://example.github.io/tcg-catalog/images/promo/SV-P/SV-P_001.webp"

    # 設定画面の「今すぐ更新」（取得は差し替え）。2回やっても重複しない
    class Resp:
        text = TCG_HTML
        url = "https://example.github.io/tcg-catalog/"

        def raise_for_status(self):
            pass
    import httpx
    monkeypatch.setattr(httpx, "get", lambda *a, **k: Resp())
    for _ in range(2):
        r = admin_client.post("/settings/catalog", data={"url": "https://example.github.io/tcg-catalog/"})
        assert r.status_code == 303
    db.expire_all()
    assert db.query(CardCatalog).count() == 3
    # 日本語のセット名でも絞り込める
    js = admin_client.get("/api/catalog/search?number=201&set=ポケモンカード").json()
    assert [c["model_id"] for c in js["candidates"]] == ["SV2a-201"] and js["candidates"][0]["image_url"]
    assert "3</b>枚" in admin_client.get("/settings").text


def test_description_follows_business_policy(admin_client, db, users):
    from app.models import Item
    from app.services import listing as listing_svc
    for cat, sealed in (("psa", False), ("single", False), ("box", True)):
        it = Item(category=cat, cert_number="5" * 8 if cat == "psa" else None, sku=f"T-{cat}", model_id="SV2a-201",
                  card_name_en="X", condition="nm" if cat == "single" else None)
        db.add(it)
        db.flush()
        _title, desc, _specs, _v = listing_svc.render(db, it)
        assert "Import duties and taxes are included (DDP)" in desc
        assert "eBay Money Back Guarantee" in desc and "within 48 hours of delivery" in desc
        assert "Immediate payment is required for Buy It Now purchases." in desc
        assert ("Sealed products" in desc) is sealed


def test_migration_refreshes_only_unedited_templates(env):
    import sqlite3
    from alembic import command
    from alembic.config import Config
    from app.config import get_config
    from app.db import reset_engine
    db_path = get_config().db_path
    reset_engine()
    ac = Config("alembic.ini")
    command.downgrade(ac, "0004")
    con = sqlite3.connect(db_path)
    con.execute("INSERT INTO templates (key, body, version, updated_at) VALUES ('psa.description', 'OLD', 1, '2026-01-01')")
    con.execute("INSERT INTO templates (key, body, version, updated_at) VALUES ('box.description', 'MINE', 3, '2026-01-01')")
    con.commit()
    con.close()
    command.upgrade(ac, "head")
    con = sqlite3.connect(db_path)
    rows = dict(con.execute("SELECT key, body FROM templates").fetchall())
    assert "Business" not in rows["psa.description"] and "DDP" in rows["psa.description"]
    assert rows["box.description"] == "MINE"



def test_sealed_csv_matches_ebay_templates(admin_client, db, env, users):
    """BOX・パックは Sealed のカテゴリ・New・*C:Set で出力し、テンプレートに無い列を出さない。"""
    from tests.test_flow import ebay_template_columns
    # セット名が無いパック・BOX は登録できない（eBay の必須項目）
    r = intake(admin_client, category="box", card_name_en="Mystery Box")
    assert r.status_code == 400
    box = done_id(intake(admin_client, category="box", card_name_en="Pokemon Card 151 Booster Box", set_id="SV2a",
                         set_name="Pokémon Card 151"))
    pack = done_id(intake(admin_client, category="pack", card_name_en="Pokemon Card 151 Booster Pack",
                          set_name="Pokémon Card 151", quantity="5"))
    d1 = ready_item(admin_client, box, start="150")
    d2 = ready_item(admin_client, pack, start="12")
    r = admin_client.post("/listings/export", data={"draft_id": [str(d1), str(d2)]})
    batch = r.headers["location"].split("batch=")[1]
    rows = list(csv.reader(io.StringIO(admin_client.get(f"/exports/{batch}.csv").content.decode("utf-8-sig"))))[1:]
    header = rows[0]
    recs = {row[header.index("CustomLabel")]: dict(zip(header, row)) for row in rows[1:]}
    b, p = recs[f"X-{box:05d}"], recs[f"P-{pack:05d}"]
    assert b["*Category"] == "261044" and b["*ConditionID"] == "1000" and b["C:Configuration"] == "Box"
    assert p["*Category"] == "183456" and p["*ConditionID"] == "1000" and p["C:Configuration"] == "Pack"
    assert b["*C:Set"] == "Pokémon Card 151" and p["*Quantity"] == "5"
    assert b["CD:Professional Grader - (ID: 27501)"] == "" and b["CD:Card Condition - (ID: 40001)"] == ""
    assert "Sealed products" in b["*Description"]
    unknown = [h for h in header if h not in ebay_template_columns() and not h.startswith("*Action(")]
    assert unknown == [], unknown


def test_regenerate_draft_after_csv_export(admin_client, db, env, users):
    """出品準備完了（CSV出力済み）の在庫でも「テンプレートから再生成」と出品形式の変更ができる。"""
    from app.models import Item, ListingDraft
    item_id = done_id(intake(admin_client, category="single", model_id="SV4M-78", condition="nm",
                             card_name_en="Iron Jugulis"))
    d1 = ready_item(admin_client, item_id, start="36")
    r = admin_client.post("/listings/export", data={"draft_id": str(d1)})
    assert r.status_code == 303

    r = admin_client.post("/listings/generate", data={"item_id": str(item_id)})
    assert r.status_code == 303 and r.headers["location"].startswith("/drafts/"), r.headers
    d2 = int(r.headers["location"].rsplit("/", 1)[1])
    db.expire_all()
    assert db.get(Item, item_id).status == "PRICED"
    assert db.get(ListingDraft, d1).status == "superseded"  # 古い（CSV出力済み）下書きは一覧から外れる
    assert admin_client.post(f"/drafts/{d2}/confirm").status_code == 303
    page = admin_client.get("/listings?lane=fixed").text
    assert f'value="{d2}"' in page and f'value="{d1}"' not in page

    # 出品準備完了の在庫を別のレーンに移せる
    db.expire_all()
    v = db.get(Item, item_id).version
    r = admin_client.post(f"/items/{item_id}/format", data={"listing_format": "auction", "version": str(v)})
    assert r.status_code == 303
    db.expire_all()
    it = db.get(Item, item_id)
    assert it.status == "PRICED" and it.listing_format == "auction"


def test_bulk_ar_lot(admin_client, db, env, users):
    """バルクは AR まとめ売りが標準：枚数必須、収録セットが1つならセット名、複数なら Mixed Sets（Set は空）。"""
    from app.models import Item
    from app.services import listing as listing_svc
    p = env / "sets.csv"
    p.write_text("model_id,set_name,name_jp,name_en\nSV4M-78,Future Flash,テツノコウベ,Iron Jugulis\n"
                 "SV5K-80,Wild Force,X,Y\n", encoding="utf-8")
    from app.services import importers
    importers.import_cards(db, p)
    db.commit()
    assert intake(admin_client, category="bulk").status_code == 400  # 枚数なし

    one = done_id(intake(admin_client, category="bulk", card_count="10", lot_sets="SV4M", condition="nm"))
    two = done_id(intake(admin_client, category="bulk", card_count="12", lot_sets="SV4M,SV5K,SV4M", rarity="sar"))
    db.expire_all()
    a, b = db.get(Item, one), db.get(Item, two)
    assert a.rarity == "AR" and a.card_count == 10 and a.set_name == "Future Flash" and a.card_name_jp == "ARまとめ 10枚 Future Flash"
    assert b.rarity == "SAR" and [s["id"] for s in b.lot_sets] == ["SV4M", "SV5K"] and b.set_id is None
    assert b.card_name_jp == "SARまとめ 12枚 複数弾"

    t1, d1, s1, _ = listing_svc.render(db, a)
    assert t1 == "Japanese Pokemon Card AR Lot of 10 Future Flash"
    assert s1["Set"] == "Future Flash" and s1["Number of Cards"] == "10" and s1["Card Condition"] == "Near Mint or Better"
    t2, d2, s2, _ = listing_svc.render(db, b)
    assert t2 == "Japanese Pokemon Card SAR Lot of 12 Mixed Sets"
    assert "Set" not in s2 and "Card Condition" not in s2  # 複数弾は Set を入れない（eBay で任意項目）
    assert "Sets included: Future Flash, Wild Force" in d2
    db.commit()  # render がテンプレートを保存するので、画面からの更新の前に確定しておく

    # 在庫詳細で収録セットを直せる
    r = admin_client.post(f"/items/{two}/edit", data={"version": str(b.version), "lot_sets": "SV5K"})
    assert r.status_code == 303
    db.expire_all()
    assert db.get(Item, two).set_name == "Wild Force"


def test_draft_preview_shows_photos_in_order(admin_client, db, users):
    from tests.test_flow import register
    item_id = done_id(register(admin_client, cert="81818181"))
    draft_id = ready_item(admin_client, item_id, start="50")
    page = admin_client.get(f"/drafts/{draft_id}").text
    assert "1枚目＝メイン" in page and 'title="1. 表"' in page and 'title="2. 裏"' in page and 'id="lightbox"' in page


def test_back_photo_is_optional(admin_client, db, env, users):
    """裏面は任意：表だけで撮影済みになり、写真1枚のCSVまで出せる。"""
    from app.models import Item
    r = intake(admin_client, category="single", model_id="SV2a-025", card_name_en="Pikachu", condition="nm")
    item_id = done_id(r)
    page = admin_client.get(f"/photos/{item_id}").text
    assert "裏（任意）" in page and "裏（必須）" not in page
    upload(admin_client, item_id, "front", n=1)
    run_jobs()
    db.expire_all()
    assert db.get(Item, item_id).status == "PHOTOGRAPHED"
    admin_client.post(f"/pricing/{item_id}/approve", data={"start": "20"})
    r = admin_client.post("/listings/generate", data={"item_id": str(item_id)})
    draft_id = int(r.headers["location"].rsplit("/", 1)[1])
    assert admin_client.post(f"/drafts/{draft_id}/confirm").status_code == 303
    r = admin_client.post("/listings/export", data={"draft_id": str(draft_id)})
    batch = r.headers["location"].split("batch=")[1]
    rows = list(csv.reader(io.StringIO(admin_client.get(f"/exports/{batch}.csv").content.decode("utf-8-sig"))))[1:]
    pic = dict(zip(rows[0], rows[1]))["PicURL"]
    assert pic and "|" not in pic


def test_intake_items_waiting_for_back_are_moved_on(admin_client, db, env, users):
    """裏面必須だった頃に表だけ撮った在庫は、撮影一覧を開くと撮影済みに進む。"""
    from app.models import Item
    from app.services import photos
    r = intake(admin_client, category="single", model_id="SV2a-025", card_name_en="Pikachu", condition="nm")
    item_id = done_id(r)
    orig = photos.maybe_mark_photographed
    photos.maybe_mark_photographed = lambda *a, **k: None  # 旧ルール相当：表だけでは進まない
    try:
        upload(admin_client, item_id, "front", n=2)
    finally:
        photos.maybe_mark_photographed = orig
    db.expire_all()
    assert db.get(Item, item_id).status == "INTAKE"
    assert admin_client.get("/photos").status_code == 200
    db.expire_all()
    assert db.get(Item, item_id).status == "PHOTOGRAPHED"


def test_format_switch_on_item_detail(admin_client, db, env, users):
    """在庫詳細の出品形式カード：切り替え・数量2以上の制限・出品中は不可。"""
    import re
    from app.models import Item
    r = intake(admin_client, category="single", model_id="SV2a-025", card_name_en="Pikachu", condition="nm",
               quantity="1", listing_format="fixed")
    item_id = done_id(r)
    page = admin_client.get(f"/items/{item_id}").text
    assert 'id="format"' in page and "✓ 通常出品" in page
    v = re.search(r'name="version" value="(\d+)"', page).group(1)
    r = admin_client.post(f"/items/{item_id}/format", data={"listing_format": "auction", "version": v})
    assert r.headers["location"] == f"/items/{item_id}#format"
    db.expire_all()
    assert db.get(Item, item_id).listing_format == "auction"
    assert "✓ オークション" in admin_client.get(f"/items/{item_id}").text

    r = intake(admin_client, category="single", model_id="SV2a-025", card_name_en="Pikachu", condition="nm",
               quantity="3", listing_format="fixed")
    multi = admin_client.get(f"/items/{done_id(r)}").text
    assert "オークションにはできません" in multi

    draft_id = ready_item(admin_client, item_id, start="20", bin="40")
    from app.adapters.manual import mark_listed
    from app.models import ListingDraft, User
    mark_listed(db, db.get(User, users["admin"].id), db.get(ListingDraft, draft_id), "123456789099")
    db.commit()
    listed = admin_client.get(f"/items/{item_id}").text
    assert "出品中は変更できません" in listed and "不落札を記録" in listed


def test_photo_done_button(admin_client, db, env, users):
    """撮影完了：表が無ければ止める。あれば撮影済みにして同じ区分の次の未撮影へ。"""
    from app.models import Item
    a = done_id(intake(admin_client, category="single", model_id="SV2a-025", card_name_en="Pikachu", condition="nm"))
    b = done_id(intake(admin_client, category="single", model_id="SV2a-201", card_name_en="Charizard ex",
                       condition="nm"))
    page = admin_client.get(f"/photos/{a}").text
    assert 'id="done-btn"' in page and "撮影完了（次のシングルへ）" in page and "スキップして次へ" in page
    r = admin_client.post(f"/photos/{a}/done")
    assert r.status_code == 303 and r.headers["location"] != f"/photos/{b}"  # 表なし → 進まない
    db.expire_all()
    assert db.get(Item, a).status == "INTAKE"
    upload(admin_client, a, "front", n=5)
    r = admin_client.post(f"/photos/{a}/done")
    assert r.headers["location"] == f"/photos/{b}"
    db.expire_all()
    assert db.get(Item, a).status == "PHOTOGRAPHED"
    upload(admin_client, b, "front", n=6)
    r = admin_client.post(f"/photos/{b}/done")
    assert r.headers["location"] == "/photos?category=single"  # 最後の1件は一覧へ


def test_delete_item(admin_client, staff_client, db, env, users):
    """在庫の削除：Lv2・確認つき。写真ファイルも消え、ID（SKU番号）は再利用しない。出品履歴があれば消せない。"""
    import re
    from app.config import get_config
    from app.models import AuditLog, Item, Photo
    a = done_id(intake(admin_client, category="single", model_id="SV2a-025", card_name_en="Pikachu", condition="nm",
                       purchase_price_jpy="777"))
    upload(admin_client, a, "front", n=11)
    run_jobs()
    db.expire_all()
    it = db.get(Item, a)
    files = [get_config().images_dir / p.original_path for p in it.photos] + \
        [get_config().public_images_dir / p.processed_path for p in it.photos]
    assert all(f.exists() for f in files)
    page = admin_client.get(f"/items/{a}").text
    assert "この在庫を削除" in page and "本当に削除しますか" in page
    staff_page = staff_client.get(f"/items/{a}").text
    assert "この在庫を削除" not in staff_page and "777" not in staff_page  # Lv1 には出さない（仕入値も漏らさない）
    assert staff_client.post(f"/items/{a}/delete", data={"confirm": it.sku}).status_code in (303, 403)
    db.expire_all()
    assert db.get(Item, a) is not None
    v = re.search(r'name="version" value="(\d+)"', page).group(1)
    admin_client.post(f"/items/{a}/delete", data={"version": v, "confirm": ""})  # 確認なし → 消さない
    db.expire_all()
    assert db.get(Item, a) is not None
    r = admin_client.post(f"/items/{a}/delete", data={"version": v, "confirm": it.sku})
    assert r.headers["location"] == "/items"
    db.expire_all()
    assert db.get(Item, a) is None and db.query(Photo).filter_by(item_id=a).count() == 0
    assert not any(f.exists() for f in files)
    log = db.query(AuditLog).filter_by(target_type="item", target_id=str(a), action="item.delete").one()
    assert "Pikachu" in str(log.before_json)
    # 一番新しい在庫を消しても、次の在庫は同じ番号にならない
    b = done_id(intake(admin_client, category="single", model_id="SV2a-025", card_name_en="Pikachu", condition="nm"))
    assert b > a

    # 出品したことがある在庫は削除できない
    draft_id = ready_item(admin_client, b, start="20")
    from app.adapters.manual import mark_listed
    from app.models import ListingDraft, User
    mark_listed(db, db.get(User, users["admin"].id), db.get(ListingDraft, draft_id), "123456789077")
    db.commit()
    page = admin_client.get(f"/items/{b}").text
    assert "この在庫を削除" not in page and "履歴を残すため削除できません" in page
    v = re.search(r'name="version" value="(\d+)"', page).group(1)
    admin_client.post(f"/items/{b}/delete", data={"version": v, "confirm": db.get(Item, b).sku})
    db.expire_all()
    assert db.get(Item, b) is not None


def test_single_eu_economy_shipping(admin_client, db, env, users):
    """シングル：説明文は欧州（エコノミー無料／スタンダード$5／SpeedPAK $23）と米国（エコノミー$10／SpeedPAK $32）、CSV の配送ポリシーはシングル用。"""
    from app.models import Item
    from app.services import listing as listing_svc
    single = Item(category="single", sku="T-s1", model_id="SV2a-201", card_name_en="X", condition="nm")
    psa = Item(category="psa", cert_number="66666666", sku="66666666", model_id="SV2a-201", card_name_en="X")
    db.add_all([single, psa])
    db.flush()
    desc = listing_svc.render(db, single)[1]
    assert "Economy International Shipping &ndash; <b>FREE</b>" in desc and "US $5.00" in desc and "US $23.00" in desc
    assert "Economy Shipping from outside US &ndash; US $10.00" in desc and "US $32.00" in desc
    assert "<b>United States</b> and <b>Europe</b>" in desc and "FedEx or DHL" not in desc
    assert "For tracked services" in desc and "Import duties and taxes are included (DDP)" in desc
    pdesc = listing_svc.render(db, psa)[1]
    assert "FedEx or DHL" in pdesc and "SpeedPAK" not in pdesc
    db.rollback()  # 描画で作られたテンプレートは捨てる（アプリ側の書き込みと競合させない）
    # CSV：シングルはシングル用の配送ポリシー、PSA は従来どおり
    d1 = ready_item(admin_client, done_id(intake(admin_client, category="single", model_id="SV2a-025",
                                                 card_name_en="Pikachu", condition="nm")), start="20")
    r = admin_client.post("/listings/export", data={"draft_id": str(d1)})
    batch = r.headers["location"].split("batch=")[1]
    rows = list(csv.reader(io.StringIO(admin_client.get(f"/exports/{batch}.csv").content.decode("utf-8-sig"))))[1:]
    assert dict(zip(rows[0], rows[1]))["ShippingProfileName"] == "Torelis Single US + EU"
    from app.settings_store import get_setting
    assert listing_svc.shipping_profile_for(get_setting(db, "listing_defaults"), "psa") == "Torelis Free Express (DDP)"


def test_tracking_options_for_post_and_untracked(db):
    import pytest
    from app.services import orders as order_svc
    from app.services.errors import ServiceError
    assert order_svc.validate_tracking(db, "NONE", "") == order_svc.NO_TRACKING
    assert order_svc.validate_tracking(db, "JPPOST", "rr 123456789 jp") == "RR123456789JP"
    assert order_svc.validate_tracking(db, "SPEEDPAK", "EE1234567890XX") == "EE1234567890XX"
    with pytest.raises(ServiceError):
        order_svc.validate_tracking(db, "JPPOST", "")
    with pytest.raises(ServiceError):
        order_svc.validate_tracking(db, "JPPOST", "12345")
    assert "japanpost" in order_svc.tracking_url("JPPOST", "RR123456789JP")
    assert order_svc.tracking_url("NONE", "x") == ""


def test_pack_uses_us_eu_shipping(db):
    from app.models import Item
    from app.services import listing as listing_svc
    from app.settings_store import get_setting
    pack = Item(category="pack", sku="P-T1", set_id="M6a", set_name="30th Celebration", card_name_en="30th Celebration")
    box = Item(category="box", sku="X-T1", set_id="M6a", set_name="30th Celebration", card_name_en="30th Celebration")
    db.add_all([pack, box])
    db.flush()
    pdesc = listing_svc.render(db, pack)[1]
    assert "Economy Shipping from outside US &ndash; US $10.00" in pdesc and "FedEx or DHL" not in pdesc
    assert "Sealed products" in pdesc and "For tracked services" in pdesc
    assert "FedEx or DHL" in listing_svc.render(db, box)[1]
    d = get_setting(db, "listing_defaults")
    assert listing_svc.shipping_profile_for(d, "pack") == "Torelis Single US + EU"
    assert listing_svc.shipping_profile_for(d, "box") == "Torelis Free Express (DDP)"
    assert listing_svc.shipping_profile_for(d, "bulk") == "Torelis Single US + EU"
    bulk = Item(category="bulk", sku="L-T1", card_name_en="Japanese AR Lot", rarity="AR", card_count=20)
    db.add(bulk)
    db.flush()
    bdesc = listing_svc.render(db, bulk)[1]
    assert "Economy Shipping from outside US &ndash; US $10.00" in bdesc and "FedEx or DHL" not in bdesc
    assert "For tracked services" in bdesc


def test_handling_days_setting_drives_all_descriptions(admin_client, db, env, users):
    """「発送までの営業日数」を1か所で変えると、全区分の説明文と発送期限に反映される。"""
    from app.models import Item
    from app.services import listing as listing_svc
    from app.settings_store import get_setting
    page = admin_client.get("/settings").text
    assert 'name="ship_business_days"' in page
    r = admin_client.post("/settings/general", data={"ship_business_days": "3", "ship_due_notify_hour_jst": "9",
                                                     "ship_due_escalate_hour_jst": "12", "ebay_adapter": "manual"})
    assert r.status_code == 303
    db.expire_all()
    assert get_setting(db, "ship_business_days") == 3
    assert get_setting(db, "listing_defaults")["dispatch_time_max"] == 3
    for cat in ("psa", "single", "pack", "box", "bulk"):
        it = Item(category=cat, sku=f"H-{cat}", cert_number="7" * 8 if cat == "psa" else None, card_name_en="X",
                  set_name="S", condition="nm" if cat == "single" else None)
        db.add(it)
        db.flush()
        desc = listing_svc.render(db, it)[1]
        assert "within 3 business days of cleared payment" in desc and "2 business days" not in desc, cat
    db.rollback()


def test_migration_keeps_custom_handling_days(env):
    """テンプレートを「within 3 business days」に編集していたら、移行後は設定が 3 になり、文は変数になる。"""
    import sqlite3
    from alembic import command
    from alembic.config import Config
    from app.config import get_config
    path = get_config().db_path
    cfg = Config("alembic.ini")
    command.downgrade(cfg, "0013")
    con = sqlite3.connect(path)
    con.execute("INSERT OR REPLACE INTO templates (key, body, version, updated_at) VALUES "
                "('pack.description', 'ship within 3 business days now', 2, CURRENT_TIMESTAMP)")
    con.commit()
    con.close()
    command.upgrade(cfg, "head")
    con = sqlite3.connect(path)
    body = con.execute("SELECT body FROM templates WHERE key='pack.description'").fetchone()[0]
    val = con.execute("SELECT value FROM settings WHERE key='ship_business_days'").fetchone()[0]
    con.close()
    assert body == "ship within {{ handling_days }} business days now" and str(val) == "3"


def test_draft_quantity_edit_and_listing_sell_out(admin_client, db, env, users):
    """出品プレビューで数量を在庫以下に変えられる。出品分が売り切れたら在庫が残っていても価格確定に戻る。"""
    import re
    from app.adapters.manual import mark_listed
    from app.models import Item, Listing, ListingDraft, User
    iid = done_id(intake(admin_client, category="single", model_id="SV2a-025", card_name_en="Pikachu",
                         condition="nm", quantity="3", listing_format="fixed"))
    upload(admin_client, iid, "front", n=31)
    run_jobs()
    admin_client.post(f"/pricing/{iid}/approve", data={"start": "10"})
    r = admin_client.post("/listings/generate", data={"item_id": str(iid)})
    did = int(r.headers["location"].rsplit("/", 1)[1])
    page = admin_client.get(f"/drafts/{did}").text
    assert 'name="quantity"' in page and 'max="3"' in page
    admin_client.post(f"/drafts/{did}/edit", data={"title": "Pikachu test", "quantity": "5"})  # 在庫超え → 不可
    db.expire_all()
    assert db.get(ListingDraft, did).quantity == 3
    admin_client.post(f"/drafts/{did}/edit", data={"title": "Pikachu test", "quantity": "2"})
    db.expire_all()
    assert db.get(ListingDraft, did).quantity == 2
    assert admin_client.post(f"/drafts/{did}/confirm").status_code == 303
    r = admin_client.post("/listings/export", data={"draft_id": str(did)})
    batch = r.headers["location"].split("batch=")[1]
    rows = list(csv.reader(io.StringIO(admin_client.get(f"/exports/{batch}.csv").content.decode("utf-8-sig"))))[1:]
    assert dict(zip(rows[0], rows[1]))["*Quantity"] == "2"
    db.expire_all()
    lst = mark_listed(db, db.get(User, users["admin"].id), db.get(ListingDraft, did), "123456789055")
    db.commit()
    assert lst.quantity == 2
    lid = lst.id
    # 出品分（2）を超える販売は記録できない
    r = admin_client.post(f"/listings/{lid}/sale", data={"buyer": "b", "amount": "30", "ship_country": "US",
                                                         "quantity": "3", "paid": "1"})
    db.expire_all()
    assert db.get(Item, iid).quantity_sold == 0
    admin_client.post(f"/listings/{lid}/sale", data={"buyer": "b", "amount": "20", "ship_country": "US",
                                                     "quantity": "2", "paid": "1"})
    db.expire_all()
    it = db.get(Item, iid)
    assert it.remaining == 1 and it.status == "PRICED" and db.get(Listing, lid).status == "sold"
    # 残り1点はそのまま再出品できる（価格は承認済みのまま）
    r = admin_client.post("/listings/generate", data={"item_id": str(iid)})
    did2 = int(r.headers["location"].rsplit("/", 1)[1])
    db.expire_all()
    assert db.get(ListingDraft, did2).quantity == 1


def test_pack_item_specifics_editable(admin_client, db, env, users):
    """パックだけ、出品プレビューで Item Specifics を編集できる（追加・変更・削除）。Game・Set は必須。"""
    from app.models import ListingDraft
    pid = done_id(intake(admin_client, category="pack", set_id="SV2a", set_name="Pokemon Card 151",
                         card_name_en="Pokemon Card 151", quantity="1", listing_format="fixed"))
    upload(admin_client, pid, "front", n=41)
    run_jobs()
    admin_client.post(f"/pricing/{pid}/approve", data={"start": "15"})
    r = admin_client.post("/listings/generate", data={"item_id": str(pid)})
    did = int(r.headers["location"].rsplit("/", 1)[1])
    page = admin_client.get(f"/drafts/{did}").text
    assert 'action="/drafts/%d/specifics"' % did in page and 'name="spec_key"' in page
    specs = dict(db.get(ListingDraft, did).item_specifics_json)
    pairs = [(k, v) for k, v in specs.items() if k != "Manufacturer"]  # Manufacturer を削除
    pairs = [(k, "2023" if False else v) for k, v in pairs] + [("Year Manufactured", "2023"), ("", "")]
    data = {"spec_key": [k for k, _ in pairs], "spec_val": [v for _, v in pairs]}
    r = admin_client.post(f"/drafts/{did}/specifics", data=data)
    assert r.headers["location"].endswith("#specifics")
    db.expire_all()
    new = db.get(ListingDraft, did).item_specifics_json
    assert new["Year Manufactured"] == "2023" and "Manufacturer" not in new and new["Game"] == "Pokémon TCG"
    # 必須（Set）を空にはできない・65文字超は不可
    bad = {"spec_key": ["Game", "Set"], "spec_val": ["Pokémon TCG", ""]}
    admin_client.post(f"/drafts/{did}/specifics", data=bad)
    too_long = {"spec_key": ["Game", "Set", "Features"], "spec_val": ["Pokémon TCG", "151", "x" * 66]}
    admin_client.post(f"/drafts/{did}/specifics", data=too_long)
    db.expire_all()
    assert db.get(ListingDraft, did).item_specifics_json == new
    # CSV に反映
    assert admin_client.post(f"/drafts/{did}/confirm").status_code == 303
    r = admin_client.post("/listings/export", data={"draft_id": str(did)})
    batch = r.headers["location"].split("batch=")[1]
    rows = list(csv.reader(io.StringIO(admin_client.get(f"/exports/{batch}.csv").content.decode("utf-8-sig"))))[1:]
    rec = dict(zip(rows[0], rows[1]))
    assert rec["C:Year Manufactured"] == "2023" and "C:Manufacturer" not in rec and rec["*C:Set"]


def test_specifics_not_editable_for_single(admin_client, db, env, users):
    from app.models import ListingDraft
    sid = done_id(intake(admin_client, category="single", model_id="SV2a-025", card_name_en="Pikachu", condition="nm"))
    did = ready_item(admin_client, sid, start="10")
    page = admin_client.get(f"/drafts/{did}").text
    assert "/specifics" not in page
    before = dict(db.get(ListingDraft, did).item_specifics_json)
    admin_client.post(f"/drafts/{did}/specifics", data={"spec_key": ["Game", "Set"], "spec_val": ["A", "B"]})
    db.expire_all()
    assert db.get(ListingDraft, did).item_specifics_json == before


def test_bulk_item_specifics_editable(admin_client, db, env, users):
    """バルクも Item Specifics を編集できる。必須は Game だけ（Set は空でもよい）。"""
    from app.models import ListingDraft
    bid = done_id(intake(admin_client, category="bulk", card_name_en="Japanese AR Lot", rarity="AR",
                         card_count="20", condition="nm", listing_format="auction"))
    did = ready_item(admin_client, bid, start="20")
    r = admin_client.post("/listings/generate", data={"item_id": str(bid)})
    did = int(r.headers["location"].rsplit("/", 1)[1])
    page = admin_client.get(f"/drafts/{did}").text
    assert f'action="/drafts/{did}/specifics"' in page
    data = {"spec_key": ["Game", "Number of Cards", "Character"], "spec_val": ["Pokémon TCG", "25", "Pikachu"]}
    admin_client.post(f"/drafts/{did}/specifics", data=data)
    db.expire_all()
    assert db.get(ListingDraft, did).item_specifics_json == {"Game": "Pokémon TCG", "Number of Cards": "25",
                                                             "Character": "Pikachu"}
    admin_client.post(f"/drafts/{did}/specifics", data={"spec_key": ["Game"], "spec_val": [""]})  # Game は必須
    db.expire_all()
    assert db.get(ListingDraft, did).item_specifics_json["Game"] == "Pokémon TCG"
