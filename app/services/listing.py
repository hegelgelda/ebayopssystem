"""出品データの生成（F0-4）。区分ごとのテンプレート（Lv2が編集）にレコードの値を差し込む。"""
from __future__ import annotations

import json
from typing import Any

from jinja2.sandbox import SandboxedEnvironment
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit
from app.models import Category, Item, ItemStatus as IS, ListingDraft, ListingFormat as LF, Template, User
from app.services import photos as photo_svc, pricing, states
from app.services.errors import PermissionDenied, ServiceError, check_version
from app.settings_store import get_setting
from app.timeutil import utcnow

TEMPLATE_PARTS = ("title", "description", "item_specifics")
PART_LABELS = {"title": "タイトル（80文字以内に切り詰め）", "description": "説明文（HTML）",
               "item_specifics": "Item Specifics（JSON）"}

# Item Specifics：グレーダー・グレード・cert番号・未鑑定の状態は CSV の Condition Descriptor（CD: 列）で渡すので、
# ここ（C: 列）には入れない（eBay のテンプレートで、同名の項目は CD: 側を優先するよう指示されている）。
# 配送・返品・支払の文面は「Torelis eBay Business Policies v1.1」の Buyer-facing text に合わせる。
# 返品の最後の一文（eBay Money Back Guarantee）と、48時間ルールを「お願い」にとどめる書き方は変えないこと。
_SHIPPING_RETURNS = """<h3>Shipping</h3>
<ul>
  <li>All items ship from Japan within 2 business days of cleared payment.</li>
  <li>We ship exclusively via FedEx or DHL Express. <b>A tracking number is provided for every order</b> and uploaded to eBay as soon as your item ships, so you can follow your package all the way to your door. Typical delivery is 3&ndash;7 business days after dispatch.</li>
  <li>Shipping is FREE to all supported destinations.</li>
  <li><b>Import duties and taxes are included (DDP).</b> You will not be charged anything on delivery.</li>
  <li>We do not ship to PO Boxes or APO/FPO addresses. Please provide a physical address and a valid phone number for the courier.</li>
  <li>Each item is packed individually and securely. Combined shipping discounts are not available.</li>
  <li>Delivery times are estimates and may vary due to customs clearance or carrier delays.</li>
</ul>
<h3>Returns</h3>
<ul>
  <li>We do not accept returns for change of mind, including buyer's remorse or ordering mistakes.</li>
  {% if category in ('pack', 'box') %}<li><b>Sealed products:</b> Once the factory seal or shrink wrap is opened, we cannot guarantee contents, pull rates, or card condition, and opened items cannot be returned.</li>{% endif %}
  <li><b>Please inspect your item upon arrival and contact us within 48 hours of delivery</b> if there is any problem, such as damage in transit or an item that differs from the listing. Please include clear photos of the item, outer box, and packaging. Prompt reporting allows us to file a claim with the carrier in time and resolve the issue quickly.</li>
  <li>This does not affect your rights under the eBay Money Back Guarantee.</li>
</ul>
<h3>Payment</h3>
<ul>
  <li>Immediate payment is required for Buy It Now purchases.</li>
  <li>All payments are processed securely through eBay.</li>
  <li>Orders ship within 2 business days after payment is confirmed, and your tracking number will be uploaded to eBay at dispatch.</li>
</ul>
</div>"""


def _specs(d: dict[str, str]) -> str:
    return json.dumps(d, ensure_ascii=False, indent=2)


DEFAULT_TEMPLATES: dict[str, dict[str, str]] = {
    Category.PSA: {
        "title": "{{ grader }} {{ grade }} {{ card_name_en }} {{ card_number }} {{ set_name }} Japanese Pokemon Card",
        "description": """<div style="font-family:Arial,sans-serif;max-width:800px">
<h2>{{ grader }} {{ grade }} {{ card_name_en }}{% if card_name_jp %} ({{ card_name_jp }}){% endif %}</h2>
<ul>
  <li>Set: {{ set_name or set_id }}</li>
  <li>Card Number: {{ card_number }}</li>
  <li>Language: Japanese</li>
  <li>Grade: {{ grader }} {{ grade }}</li>
  <li>Certification Number: {{ cert_number }}</li>
</ul>
<p>The item you will receive is the exact card shown in the photos.</p>
""" + _SHIPPING_RETURNS,
        "item_specifics": _specs({
            "Game": "Pokémon TCG", "Card Name": "{{ card_name_en }}", "Set": "{{ set_name or set_id }}",
            "Card Number": "{{ card_number }}", "Language": "Japanese", "Year Manufactured": "{{ year }}",
            "Manufacturer": "The Pokémon Company"}),
    },
    Category.SINGLE: {
        "title": "{{ card_name_en }} {{ card_number }} {{ set_name }} Japanese Pokemon Card {{ condition_short }}",
        "description": """<div style="font-family:Arial,sans-serif;max-width:800px">
<h2>{{ card_name_en }}{% if card_name_jp %} ({{ card_name_jp }}){% endif %}</h2>
<ul>
  <li>Set: {{ set_name or set_id }}</li>
  <li>Card Number: {{ card_number }}</li>
  <li>Language: Japanese</li>
  <li>Condition: {{ condition_label }} (ungraded)</li>
  {% if quantity > 1 %}<li>Quantity available: {{ quantity }}</li>{% endif %}
</ul>
<p>{% if quantity > 1 %}Photos show the actual card; you will receive a card in the same condition.{% else %}The item you will receive is the exact card shown in the photos.{% endif %}
Shipped in a sleeve and top loader.</p>
""" + _SHIPPING_RETURNS,
        "item_specifics": _specs({
            "Game": "Pokémon TCG", "Card Name": "{{ card_name_en }}", "Set": "{{ set_name or set_id }}",
            "Card Number": "{{ card_number }}", "Language": "Japanese",
            "Manufacturer": "The Pokémon Company"}),
    },
    Category.PACK: {
        "title": "{{ card_name_en or set_name }} Booster Pack Sealed Japanese Pokemon Card",
        "description": """<div style="font-family:Arial,sans-serif;max-width:800px">
<h2>{{ card_name_en or set_name }} Booster Pack (Japanese, Sealed)</h2>
<ul>
  <li>Set: {{ set_name or set_id }}</li>
  <li>Language: Japanese</li>
  <li>Condition: Factory sealed, unopened</li>
  {% if quantity > 1 %}<li>Quantity available: {{ quantity }}</li>{% endif %}
</ul>
""" + _SHIPPING_RETURNS,
        "item_specifics": _specs({
            "Game": "Pokémon TCG", "Set": "{{ set_name or set_id }}", "Configuration": "Pack", "Number of Packs": "1",
            "Language": "Japanese", "Manufacturer": "The Pokémon Company"}),
    },
    Category.BOX: {
        "title": "{{ card_name_en or set_name }} Booster Box Sealed Japanese Pokemon Card",
        "description": """<div style="font-family:Arial,sans-serif;max-width:800px">
<h2>{{ card_name_en or set_name }} Booster Box (Japanese, Sealed)</h2>
<ul>
  <li>Set: {{ set_name or set_id }}</li>
  <li>Language: Japanese</li>
  <li>Condition: Factory sealed with shrink wrap</li>
  {% if quantity > 1 %}<li>Quantity available: {{ quantity }}</li>{% endif %}
</ul>
""" + _SHIPPING_RETURNS,
        "item_specifics": _specs({
            "Game": "Pokémon TCG", "Set": "{{ set_name or set_id }}", "Configuration": "Box", "Number of Boxes": "1",
            "Language": "Japanese", "Manufacturer": "The Pokémon Company"}),
    },
    Category.BULK: {
        "title": "{{ card_name_en or card_name_jp }} Japanese Pokemon Card Bulk Lot",
        "description": """<div style="font-family:Arial,sans-serif;max-width:800px">
<h2>{{ card_name_en or card_name_jp }} (Japanese Pokemon Card Lot)</h2>
<p>{{ memo }}</p>
<p>You will receive the exact lot shown in the photos.</p>
""" + _SHIPPING_RETURNS,
        "item_specifics": _specs({
            "Game": "Pokémon TCG", "Language": "Japanese",
            "Manufacturer": "The Pokémon Company"}),
    },
}

GRADER_FULL = {"PSA": "Professional Sports Authenticator (PSA)", "BGS": "Beckett Grading Services (BGS)",
               "CGC": "Certified Guaranty Company (CGC)"}

_env = SandboxedEnvironment(autoescape=False)


def template_key(category: str, part: str) -> str:
    return f"{category}.{part}"


def get_template(db: Session, key: str) -> Template:
    t = db.get(Template, key)
    if t is None:
        cat, part = key.split(".", 1)
        t = Template(key=key, body=DEFAULT_TEMPLATES[cat][part], version=1)
        db.add(t)
        db.flush()
    return t


def update_template(db: Session, user: User, key: str, body: str) -> Template:
    if user.level < 2:
        raise PermissionDenied("テンプレートの編集はLv2のみ可能です")
    cat, _, part = key.partition(".")
    if cat not in DEFAULT_TEMPLATES or part not in TEMPLATE_PARTS:
        raise ServiceError("不明なテンプレートです")
    try:
        _env.from_string(body)
        if part == "item_specifics":
            json.loads(body)
    except Exception as e:  # noqa: BLE001
        raise ServiceError(f"テンプレートの書式エラー: {e}")
    t = get_template(db, key)
    before = t.body
    t.body, t.version, t.updated_by = body, t.version + 1, user.id
    audit.log(db, audit.Actor.user(user), "setting", f"template:{key}", "template.update",
              before={"body": before}, after={"body": body, "version": t.version})
    return t


def raw_condition(db: Session, key: str | None) -> dict[str, str]:
    for c in get_setting(db, "raw_conditions") or []:
        if c.get("key") == key:
            return c
    return {}


def context_for(db: Session, item: Item, quantity: int | None = None) -> dict[str, Any]:
    cond = raw_condition(db, item.condition)
    return {
        "category": item.category, "sku": item.sku, "cert_number": item.cert_number or "",
        "model_id": item.model_id or "", "card_name_jp": item.card_name_jp or "",
        "card_name_en": item.card_name_en or "", "set_id": item.set_id or "", "set_name": item.set_name or "",
        "card_number": item.card_number or "", "grader": item.grader or "", "grade": item.grade or "",
        "grader_full": GRADER_FULL.get(item.grader or "", item.grader or ""), "year": item.year or "",
        "population": item.psa_population or "", "condition_label": cond.get("label", ""),
        "condition_short": cond.get("short", ""), "quantity": quantity if quantity is not None else item.remaining,
        "memo": item.memo or "",
    }


def render(db: Session, item: Item, quantity: int | None = None) -> tuple[str, str, dict[str, str], str]:
    ctx = context_for(db, item, quantity)
    tt, td, ts = (get_template(db, template_key(item.category, p)) for p in TEMPLATE_PARTS)
    title = " ".join(_env.from_string(tt.body).render(ctx).split())
    if len(title) > 80:
        title = title[:80].rsplit(" ", 1)[0] if " " in title[:80] else title[:80]
    desc = _env.from_string(td.body).render(ctx)
    raw_specs = json.loads(ts.body)
    specs = {k: " ".join(_env.from_string(str(v)).render(ctx).split()) for k, v in raw_specs.items()}
    specs = {k: v for k, v in specs.items() if v}
    version = f"{item.category}.t{tt.version}.d{td.version}.s{ts.version}"
    return title, desc, specs, version


def image_urls(item: Item) -> list[str]:
    ps = sorted([p for p in item.active_photos() if p.process_status == "done"], key=lambda p: p.seq)
    return [photo_svc.public_url(p) for p in ps]


def current_draft(db: Session, item: Item) -> ListingDraft | None:
    return db.scalar(select(ListingDraft).where(ListingDraft.item_id == item.id,
                                                ListingDraft.status != "superseded")
                     .order_by(ListingDraft.id.desc()).limit(1))


def open_drafts(db: Session, item: Item) -> list[ListingDraft]:
    return list(db.scalars(select(ListingDraft).where(ListingDraft.item_id == item.id,
                                                      ListingDraft.status.in_(["draft", "ready", "exported", "failed"]))))


def generate_draft(db: Session, user: User, item: Item) -> ListingDraft:
    if item.status not in (IS.PRICED, IS.READY):
        raise ServiceError("価格確定の在庫のみ出品データを作成できます")
    if pricing.open_quote(db, item) is not None and item.status == IS.PRICED:
        raise ServiceError("新しい価格案がまだ承認されていません（価格承認を先に済ませてください）")
    q = pricing.latest_approved(db, item)
    if q is None:
        raise ServiceError("承認済みの価格がありません")
    if q.format != item.listing_format:
        raise ServiceError(f"承認済みの価格は{LF.LABELS.get(q.format)}用です。{item.format_label}の価格を承認してください")
    if item.remaining < 1:
        raise ServiceError("出品できる数量がありません")
    qty = 1 if item.listing_format == LF.AUCTION else item.remaining
    title, desc, specs, tv = render(db, item, qty)
    defaults = get_setting(db, "listing_defaults")
    for old in db.scalars(select(ListingDraft).where(ListingDraft.item_id == item.id,
                                                     ListingDraft.status.in_(["draft", "ready", "exported", "failed"]))):
        old.status = "superseded"
    d = ListingDraft(item_id=item.id, price_quote_id=q.id, title=title, description_html=desc,
                     item_specifics_json=specs, format=item.listing_format, quantity=qty,
                     duration_days=int(defaults.get("duration_days", 7)) if item.listing_format == LF.AUCTION else 30,
                     start_price=q.approved_start, bin_price=q.approved_bin, currency=q.currency,
                     template_version=tv, status="draft")
    db.add(d)
    db.flush()
    if item.status == IS.READY:
        states.transition_item(db, item, IS.PRICED, audit.Actor.user(user), action="item.draft_regenerated")
    audit.log(db, audit.Actor.user(user), "listing", d.id, "draft.generate",
              after={"item_id": item.id, "title": title, "format": d.format, "quantity": qty, "start": d.start_price,
                     "bin": d.bin_price, "template_version": tv})
    return d


def update_draft(db: Session, user: User, draft: ListingDraft, data: dict[str, Any]) -> None:
    if draft.status not in ("draft",):
        raise ServiceError("確認済みの下書きは編集できません（再生成してください）")
    before = {}
    after = {}
    if "title" in data and data["title"] is not None:
        t = " ".join(str(data["title"]).split())
        if len(t) > 80:
            raise ServiceError("タイトルは80文字以内にしてください")
        before["title"], after["title"] = draft.title, t
        draft.title = t
    if data.get("duration_days") and draft.format == LF.AUCTION:
        before["duration_days"], after["duration_days"] = draft.duration_days, int(data["duration_days"])
        draft.duration_days = int(data["duration_days"])
    if after:
        audit.log(db, audit.Actor.user(user), "listing", draft.id, "draft.update", before=before, after=after)


def confirm_draft(db: Session, user: User, draft: ListingDraft, version: int | None) -> None:
    item = db.get(Item, draft.item_id)
    assert item is not None
    check_version(item, version)
    if draft.status != "draft":
        raise ServiceError("この下書きは確認済みです")
    if draft.format != item.listing_format:
        raise ServiceError("出品形式が変わっています。下書きを作り直してください")
    missing = [k for k in item.required_photos() if not item.photo(k)]
    if missing:
        raise ServiceError("必須の写真がありません")
    pending = [p for p in item.active_photos() if p.process_status != "done"]
    if pending:
        raise ServiceError("画像の整形が終わっていません。少し待ってから確認してください")
    draft.status = "ready"
    draft.confirmed_by, draft.confirmed_at = user.id, utcnow()
    states.transition_item(db, item, IS.READY, audit.Actor.user(user), action="item.ready")
    audit.log(db, audit.Actor.user(user), "listing", draft.id, "draft.confirm", after={"status": "ready"})


def ready_drafts(db: Session, listing_format: str | None = None) -> list[ListingDraft]:
    stmt = (select(ListingDraft).join(Item, Item.id == ListingDraft.item_id)
            .where(Item.status == IS.READY, ListingDraft.status.in_(["ready", "exported", "failed"])))
    if listing_format:
        stmt = stmt.where(ListingDraft.format == listing_format)
    return list(db.scalars(stmt.order_by(ListingDraft.id)).all())
