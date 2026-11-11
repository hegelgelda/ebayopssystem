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
