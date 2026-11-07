"""期限・担当・通知・取り込みのルール。"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import select

from app.timeutil import add_business_days, jst_to_utc_naive, ship_by_from_paid, to_jst


def test_ship_by_two_business_days_skips_holidays():
    # 2026-11-02(月) 支払 → 11-03(火) は文化の日 → 11-04(水), 11-05(木) → 11-05 23:59 JST
    paid = jst_to_utc_naive(datetime(2026, 11, 2, 15, 0))
    due = to_jst(ship_by_from_paid(paid))
    assert (due.date(), due.hour, due.minute) == (date(2026, 11, 5), 23, 59)
    # 金曜支払 → 翌週火曜
    assert add_business_days(date(2026, 11, 6), 2) == date(2026, 11, 10)


def test_duty_resolution(db, users):
    from app.models import DutyOverride, DutySchedule
    from app.services import duty
    d = date(2026, 11, 4)  # 水曜
    db.add(DutySchedule(weekday=d.weekday(), user_id=users["staff"].id))
    db.commit()
    assert duty.duty_user_for(db, d).id == users["staff"].id
    db.add(DutyOverride(date=d, user_id=users["lv2b"].id))
    db.commit()
    assert duty.duty_user_for(db, d).id == users["lv2b"].id
    # 祝日（11/3 文化の日・火曜）は不在
    db.add(DutySchedule(weekday=1, user_id=users["staff"].id))
    db.commit()
    assert duty.duty_user_for(db, date(2026, 11, 3)) is None
    # 撮影担当が未設定なら曜日担当が兼ねる
    assert duty.role_users(db, "is_photographer", d)[0].id == users["lv2b"].id


def test_notification_dedupe_and_escalation(db, users):
    from app.models import Item, Notification, Order, Outbox
    from app.services import notify
    from app.timeutil import utcnow
    it = Item(cert_number="1", model_id="X-1")
    db.add(it)
    db.flush()
    o = Order(item_id=it.id, status="AWAITING_PAYMENT")
    db.add(o)
    db.flush()
    n1 = notify.notify(db, "order_sold", "order", o.id, "sold")
    n2 = notify.notify(db, "order_sold", "order", o.id, "sold again")
    assert n1 is not None and n2 is None
    assert n1.escalate_at - n1.sent_at == timedelta(minutes=120)
    assert notify.run_escalations(db, utcnow() + timedelta(minutes=60)) == 0
    assert notify.run_escalations(db, utcnow() + timedelta(minutes=121)) == 1
    assert notify.run_escalations(db, utcnow() + timedelta(minutes=200)) == 0
    esc = [ob for ob in db.scalars(select(Outbox)) if "エスカレーション" in ob.payload.get("content", "")]
    assert len(esc) == 1 and esc[0].payload["mention_roles"] == ["lv2"]
    # 対応済みならエスカレーションしない
    n3 = notify.notify(db, "transit_exception", "order", o.id, "ex")
    notify.acknowledge(db, n3, users["staff"], __import__("app.audit", fromlist=["Actor"]).Actor.user(users["staff"]))
    assert notify.run_escalations(db, utcnow() + timedelta(days=1)) == 0
    assert db.get(Notification, n3.id).escalated_at is None


def test_escalation_minutes_configurable(db, users):
    from app.models import Item
    from app.services import notify
    from app.settings_store import get_setting, set_setting
    esc = get_setting(db, "escalation_minutes")
    esc["order_sold"] = 15
    set_setting(db, "escalation_minutes", esc, users["admin"].id)
    it = Item(cert_number="2", model_id="X-2")
    db.add(it)
    db.flush()
    n = notify.notify(db, "order_sold", "order", 999, "x")
    assert n.escalate_at - n.sent_at == timedelta(minutes=15)


def test_payment_and_ship_deadline_notifications(db, users):
    from app.models import Item, Notification, Order
    from app.services import orders
    from app.timeutil import utcnow
    it = Item(cert_number="3", model_id="X-3")
    db.add(it)
    db.flush()
    now = utcnow()
    o = Order(item_id=it.id, status="AWAITING_PAYMENT", sold_at=now - timedelta(days=3, hours=1),
              payment_due=now + timedelta(hours=23))
    db.add(o)
    db.flush()
    orders.run_deadline_checks(db, now)
    ev = {n.event_type for n in db.scalars(select(Notification))}
    assert "payment_reminder" in ev and "payment_overdue" not in ev
    orders.run_deadline_checks(db, now + timedelta(hours=24))
    ev = {n.event_type for n in db.scalars(select(Notification))}
    assert "payment_overdue" in ev


def test_import_market_and_reference_values(db, env, users):
    from app.models import Item
    from app.services import importers, pricing
    p = env / "prices.csv"
    p.write_text("model_id,source,metric,value,currency,count,as_of\n"
                 "SV2a-201,courtyard_p2p,median,350,USD,12,2026-10-31\n"
                 "SV2a-201,pokeca,psa10_price,48000,JPY,,2026-10-31\n", encoding="utf-8")
    assert importers.import_market_long(db, p) == (2, 0)
    assert importers.import_market_long(db, p) == (0, 2)  # 冪等
    it = Item(cert_number="4", model_id="SV2a-201", purchase_price_jpy=30000)
    db.add(it)
    db.flush()
    refs = pricing.reference_values(db, it)
    assert refs["values"]["courtyard_p2p.median"]["usd"] == "350.00"
    start, bin_, note = pricing.suggest(db, it, refs)
    assert start == pricing.breakeven_usd(db, 30000, __import__("decimal").Decimal(refs["fx"]["usdjpy"]))
    assert bin_ == 420  # 350 × 1.2


def test_import_cards_constant(db, env):
    from app.models import CardCatalog
    from app.services import importers
    html = env / "buyback.html"
    html.write_text("""<script>
    const CARDS = [
      {set_id: 'SV2a', number: '201', name_jp: 'リザードンex', name_en: 'Charizard ex'}, // comment
      {model_id: "SV4a-349", name_jp: "ピカチュウ", name_en: "Pikachu",},
    ];
    </script>""", encoding="utf-8")
    assert importers.import_cards(db, html) == 2
    db.commit()
    assert db.get(CardCatalog, "SV2a-201").name_en == "Charizard ex"
    assert db.get(CardCatalog, "SV4a-349").set_id == "SV4a"


def test_lookup_courtyard_catalog(db, env):
    from app.services import cardinfo, importers
    cards = env / "cards.csv"
    cards.write_text("model_id,name_jp,name_en,set_name\nSV2a-201,リザードンex,Charizard ex,151\n", encoding="utf-8")
    importers.import_cards(db, cards)
    cat = env / "courtyard.csv"
    cat.write_text("cert_number,model_id,grade\n87654321,SV2a-201,10\n", encoding="utf-8")
    importers.import_cert_catalog(db, cat)
    db.commit()
    res = cardinfo.lookup(db, "87654321", allow_network=False)
    assert res["source"] == "courtyard"
    assert res["fields"]["card_name_jp"] == "リザードンex" and res["fields"]["grade"] == "10"
    assert cardinfo.normalize_cert("https://www.psacard.com/cert/87654321/psa") == "87654321"


def test_psa_html_parser():
    from app.services.cardinfo import parse_psa_cert_html
    html = """<dl><dt>Item Grade</dt><dd>GEM MT 10</dd><dt>Subject</dt><dd>CHARIZARD ex</dd>
    <dt>Card Number</dt><dd>201</dd><dt>PSA Population</dt><dd>1,234</dd></dl>"""
    d = parse_psa_cert_html(html)
    assert d["grade"] == "10" and d["card_name_en"] == "CHARIZARD ex" and d["psa_population"] == 1234


def test_backup_and_restore(env, db, users):
    from app.config import get_config
    from app.services import backup
    cfg = get_config()
    (cfg.public_images_dir / "x").mkdir(parents=True, exist_ok=True)
    (cfg.public_images_dir / "x" / "1.jpg").write_bytes(b"jpg")
    db.close()
    res = backup.create_backup()
    stamp = res["db"].rsplit("db-", 1)[1][:-8]
    (cfg.public_images_dir / "x" / "1.jpg").unlink()
    from app.db import reset_engine
    reset_engine()
    backup.restore(stamp)
    assert (cfg.public_images_dir / "x" / "1.jpg").read_bytes() == b"jpg"
    from app.db import SessionLocal
    from app.models import User
    with SessionLocal() as s:
        assert s.scalar(select(User).where(User.display_name == "管理者A")) is not None


def test_backup_prune_retention(env, monkeypatch):
    from datetime import datetime, timedelta
    from app.config import get_config
    from app.services import backup
    from app.timeutil import JST
    cfg = get_config()
    cfg.backups_dir.mkdir(parents=True, exist_ok=True)
    now = datetime(2026, 11, 30, 12, 0, tzinfo=JST)

    def make(days_ago):
        stamp = (now - timedelta(days=days_ago)).strftime("%Y%m%d-%H%M%S")
        for name in (f"db-{stamp}.sqlite3", f"images-{stamp}.tar.gz"):
            (cfg.backups_dir / name).write_bytes(b"x")
        return stamp

    old, mid, new = make(40), make(10), make(1)
    # オブジェクトストレージ未設定：VPS内が唯一の保存先なので30日分残す
    backup.prune(now)
    left = {p.name for p in cfg.backups_dir.iterdir()}
    assert f"db-{old}.sqlite3" not in left and f"db-{mid}.sqlite3" in left and f"db-{new}.sqlite3" in left

    # 設定あり：VPS内は3日分、オブジェクトストレージは30日分
    deleted = []

    class FakeS3:
        def list_objects_v2(self, Bucket, Prefix):
            return {"Contents": [{"Key": f"{Prefix}db-{s}.sqlite3"} for s in (old, mid, new)]}

        def delete_object(self, Bucket, Key):
            deleted.append(Key.rsplit("/", 1)[-1])

    monkeypatch.setattr(backup, "_s3", lambda: FakeS3())
    backup.prune(now)
    left = {p.name for p in cfg.backups_dir.iterdir()}
    assert left == {f"db-{new}.sqlite3", f"images-{new}.tar.gz"}
    assert deleted == [f"db-{old}.sqlite3"]


def test_psa_api_lookup_fills_grade_pop_and_model(db, env, monkeypatch):
    """PSA API の結果でグレード・Population が埋まり、Brand のセットIDと型番からカード一覧のカードを特定する。"""
    import httpx
    from app.config import get_config
    from app.models import ApiHealth
    from app.services import cardinfo, importers
    cards = env / "cards.csv"
    cards.write_text("model_id,set_name,name_jp,name_en\nSV4M-78,Future Flash,テツノコウベ,Iron Jugulis\n"
                     "SV4K-78,Ancient Roar,テツノコウベ,Iron Jugulis\n", encoding="utf-8")
    importers.import_cards(db, cards)
    db.commit()
    monkeypatch.setattr(get_config(), "psa_api_token", "tok")

    class R:
        text = ""

        def __init__(self, code, js=None):
            self.status_code, self._js = code, js

        def json(self):
            return self._js

        def raise_for_status(self):
            pass

    ok = {"PSACert": {"CertNumber": "12345678", "Year": "2023", "Brand": "POKEMON JAPANESE SV4M-FUTURE FLASH",
                      "CardNumber": "078", "Subject": "IRON JUGULIS", "Variety": "ART RARE",
                      "CardGrade": "GEM MT 10", "TotalPopulation": 1234},
          "IsValidRequest": True, "ServerMessage": "Request successful"}
    monkeypatch.setattr(httpx, "get", lambda url, **k: R(200, ok))
    res = cardinfo.lookup(db, "12345678")
    f = res["fields"]
    assert res["source"] == "psa" and res["matched"] == "SV4M-78"
    assert f["grade"] == "10" and f["psa_population"] == 1234 and f["year"] == "2023"
    assert f["model_id"] == "SV4M-78" and f["card_name_jp"] == "テツノコウベ" and f["set_name"] == "Future Flash"
    assert res["psa"]["variety"] == "ART RARE"

    # トークンが無効・上限・見つからない は理由を返す
    for code, word in ((401, "トークン"), (429, "上限"), (404, "見つかりません")):
        monkeypatch.setattr(httpx, "get", lambda url, code=code, **k: R(code) if "api.psacard" in url else R(403))
        res = cardinfo.lookup(db, "12345678")
        assert res["source"] == "manual" and any(word in e for e in res["errors"]), res["errors"]
    db.commit()
    assert db.get(ApiHealth, "psa-api").consecutive_failures >= 1

    # 承認されていないアカウント（403 approved customers）は通知を鳴らさず理由だけ返す
    class R403(R):
        text = '{"Message":"Access to this API is limited to approved customers."}'
    before = db.get(ApiHealth, "psa-api").consecutive_failures
    monkeypatch.setattr(httpx, "get", lambda url, **k: R403(403))
    res = cardinfo.lookup(db, "12345678")
    assert any("承認" in e for e in res["errors"])
    db.commit()
    assert db.get(ApiHealth, "psa-api").consecutive_failures == before

    # トークンが無ければ PSA には問い合わせない（サイトは Bot 対策で取得できないため）
    monkeypatch.setattr(get_config(), "psa_api_token", "")
    def boom(*a, **k):
        raise AssertionError("network must not be used")
    monkeypatch.setattr(httpx, "get", boom)
    res = cardinfo.lookup(db, "12345678")
    assert res["source"] == "manual" and res["errors"] == []
