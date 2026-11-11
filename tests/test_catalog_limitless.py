"""Limitless TCG（日本語版）からの新弾・英語名の補完。ネットには出ず、保存したページで試す。"""
from __future__ import annotations

from pathlib import Path

import pytest

FIX = Path(__file__).parent / "fixtures" / "limitless"


class FakeResp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        return self


class FakeHttp:
    """URL → 保存したページ。SV10 は弾名の確認用に M6 と同じ形のページを作る。"""

    def __init__(self):
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        if url.endswith("/cards/jp"):
            return FakeResp((FIX / "sets.html").read_text(encoding="utf-8"))
        en = "translate=en" in url
        page = (FIX / ("m6_en.html" if en else "m6_jp.html")).read_text(encoding="utf-8")
        if "/cards/jp/SV10" in url:
            page = page.replace("/M6/", "/SV10/").replace("M6_", "SV10_").replace(
                "ストームエメラルダ", "ロケット団の栄光")
        return FakeResp(page)


@pytest.mark.real_limitless
def test_limitless_adds_new_set_and_fixes_names(db, monkeypatch):
    from app.models import CardCatalog
    from app.services import importers
    import app.timeutil as tu
    from datetime import date
    monkeypatch.setattr(tu, "today_jst", lambda *a: date(2026, 11, 11))
    # tcg-catalog 由来：SV10 は弾名が誤り、1番は英語名・レアリティが空、"001" 形式の重複行もある
    db.add_all([
        CardCatalog(model_id="SV10-1", set_id="SV10", card_number="1", name_jp="ヘラクロス", name_en="Keep Me",
                    set_name="プリズムコレクション", set_name_jp="プリズムコレクション", source="tcg-catalog"),
        CardCatalog(model_id="SV10-001", set_id="SV10", card_number="001", name_jp="ヘラクロス",
                    set_name="プリズムコレクション", source="tcg-catalog"),
    ])
    db.commit()
    http = FakeHttp()
    res = importers.import_limitless_jp(db, http=http, delay=0)
    db.commit()
    assert res["new_sets"] == ["M6"] and res["inserted"] == 3 + 2  # M6 の3枚 + SV10 の2・3番
    m6 = db.get(CardCatalog, "M6-1")
    assert (m6.name_jp, m6.name_en, m6.rarity) == ("ヘラクロス", "Heracross", "Common")
    assert (m6.set_name, m6.set_name_jp) == ("Storm Emeralda", "ストームエメラルダ")  # 英語名はタイトル用
    assert m6.image_url.endswith("/tpc/M6/M6_1_R_JP_LG.png") and m6.source == "limitless"
    keep = db.get(CardCatalog, "SV10-1")
    assert keep.name_en == "Keep Me"  # tcg-catalog の値は上書きしない
    dup = db.get(CardCatalog, "SV10-001")
    assert dup.name_en == "Heracross" and dup.rarity == "Common"  # 空欄だけ埋める（001 も同じ番号として扱う）
    assert keep.set_name == dup.set_name == "Glory of Team Rocket"  # 弾名は英語に
    assert keep.set_name_jp == dup.set_name_jp == "ロケット団の栄光"  # 誤った日本語の弾名を直す
    assert db.get(CardCatalog, "SV10-2") is not None and db.get(CardCatalog, "SV10-01") is None

    # 2回目：重複を作らない。tcg-catalog が誤った弾名に戻しても、学習済みの名前で直る
    keep.set_name = keep.set_name_jp = "プリズムコレクション"
    db.commit()
    n = db.query(CardCatalog).count()
    importers.apply_set_names(db)
    assert (keep.set_name, keep.set_name_jp) == ("Glory of Team Rocket", "ロケット団の栄光")
    importers.import_limitless_jp(db, http=FakeHttp(), delay=0)
    db.commit()
    assert db.query(CardCatalog).count() == n


@pytest.mark.real_limitless
def test_limitless_parsers():
    from app.services import importers
    sets = importers.parse_limitless_sets((FIX / "sets.html").read_text(encoding="utf-8"))
    assert [s["set_id"] for s in sets] == ["M6", "SV10"]
    assert str(sets[0]["release"]) == "2026-07-31" and sets[0]["count"] == 113
    rows = importers.parse_limitless_set_page((FIX / "m6_jp.html").read_text(encoding="utf-8"), "M6")
    assert rows[0]["number"] == "1" and rows[0]["set_name_jp"] == "ストームエメラルダ"


def test_number_search_dedupes_and_puts_new_sets_first(db, admin_client):
    from app.models import CardCatalog
    from app.settings_store import set_setting
    db.add_all([
        CardCatalog(model_id="M5-001", set_id="M5", card_number="001", name_jp="トロピウス"),
        CardCatalog(model_id="M5-1", set_id="M5", card_number="1", name_jp="トロピウス", name_en="Tropius",
                    rarity="Common"),
        CardCatalog(model_id="M6-1", set_id="M6", card_number="1", name_jp="ヘラクロス", name_en="Heracross"),
        CardCatalog(model_id="SV10-1", set_id="SV10", card_number="1", name_jp="ナゾノクサ"),
    ])
    set_setting(db, "catalog_set_release", {"M6": "2026-07-31", "M5": "2026-05-22", "SV10": "2025-04-18"}, None)
    db.commit()
    js = admin_client.get("/api/catalog/search?number=1").json()
    assert [c["model_id"] for c in js["candidates"]] == ["M6-1", "M5-1", "SV10-1"]



def test_english_names_with_accents_are_kept():
    from app.services.importers import has_japanese
    assert not has_japanese("Poké Pad") and not has_japanese("Nidoran♀") and not has_japanese("Pokémon Card 151")
    assert has_japanese("ポケパッド") and has_japanese("30th セレブレーション") and has_japanese("基本悪エネルギー")


@pytest.mark.real_limitless
def test_unlisted_items_get_english_set_name(db, admin_client, monkeypatch):
    """出品前の在庫の弾名が日本語なら英語に直り、タイトルが英語になる。"""
    from datetime import date
    import app.timeutil as tu
    from app.models import Item
    from app.services import importers, listing
    monkeypatch.setattr(tu, "today_jst", lambda *a: date(2026, 11, 11))
    it = Item(sku="S-09001", category="single", set_id="M6", card_number="1", model_id="M6-1",
              card_name_jp="ヘラクロス", card_name_en="Heracross", set_name="ストームエメラルダ", condition="nm",
              status="PRICED", listing_format="fixed")
    sold = Item(sku="S-09002", category="single", set_id="M6", card_number="2", model_id="M6-2",
                card_name_en="Surskit", set_name="ストームエメラルダ", status="SOLD", listing_format="fixed")
    db.add_all([it, sold])
    db.commit()
    res = importers.import_limitless_jp(db, http=FakeHttp(), delay=0)
    db.commit()
    assert res["items_fixed"] == 1 and it.set_name == "Storm Emeralda" and sold.set_name == "ストームエメラルダ"
    title = listing.render(db, it, 1)[0]
    assert title.startswith("Heracross 1 Storm Emeralda Japanese Pokemon Card") and not importers.has_japanese(title)


def test_promo_sets_and_basic_energy_get_english(db):
    from app.models import CardCatalog
    from app.services import importers
    db.add_all([CardCatalog(model_id="SV-P-1", set_id="SV-P", card_number="1", name_jp="ピカチュウ",
                            set_name="スカーレット&バイオレット プロモ"),
                CardCatalog(model_id="M6a-G", set_id="M6a", card_number="G", name_jp="基本草エネルギー")])
    db.commit()
    importers.apply_set_names(db)
    assert db.get(CardCatalog, "SV-P-1").set_name == "Scarlet & Violet Promo"
    assert db.get(CardCatalog, "SV-P-1").set_name_jp == "スカーレット&バイオレット プロモ"
    assert db.get(CardCatalog, "M6a-G").name_en == "Basic Grass Energy"
