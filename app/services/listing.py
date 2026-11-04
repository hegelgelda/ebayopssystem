"""出品データの生成（F0-4）。テンプレート（Lv2が編集）にレコードの値を差し込む。"""
from __future__ import annotations

import json
from typing import Any

from jinja2.sandbox import SandboxedEnvironment
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit
from app.models import Item, ItemStatus as IS, ListingDraft, Template, User
from app.services import photos as photo_svc, pricing, states
from app.services.errors import ServiceError, check_version
from app.settings_store import get_setting
from app.timeutil import utcnow

DEFAULT_TEMPLATES: dict[str, str] = {
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
<h3>Shipping</h3>
<p>Free shipping via FedEx / DHL Express with tracking. Import duties and taxes are prepaid by the seller (DDP).
Ships within 2 business days after payment.</p>
<h3>Returns</h3>
<p>Returns are not accepted. This does not affect eBay Money Back Guarantee.
If the item arrives damaged, please contact us within 48 hours of delivery.</p>
</div>""",
    "item_specifics": json.dumps({
        "Game": "Pokémon TCG",
        "Card Name": "{{ card_name_en }}",
        "Set": "{{ set_name or set_id }}",
        "Card Number": "{{ card_number }}",
        "Language": "Japanese",
        "Graded": "Yes",
        "Grade": "{{ grade }}",
        "Professional Grader": "{{ grader_full }}",
        "Certification Number": "{{ cert_number }}",
        "Manufacturer": "The Pokémon Company",
    }, ensure_ascii=False, indent=2),
}

GRADER_FULL = {"PSA": "Professional Sports Authenticator (PSA)", "BGS": "Beckett Grading Services (BGS)",
               "CGC": "Certified Guaranty Company (CGC)"}

_env = SandboxedEnvironment(autoescape=False)


def get_template(db: Session, key: str) -> Template:
    t = db.get(Template, key)
    if t is None:
        t = Template(key=key, body=DEFAULT_TEMPLATES[key], version=1)
        db.add(t)
        db.flush()
    return t


def update_template(db: Session, user: User, key: str, body: str) -> Template:
    if user.level < 2:
        from app.services.errors import PermissionDenied
        raise PermissionDenied("テンプレートの編集はLv2のみ可能です")
    if key not in DEFAULT_TEMPLATES:
        raise ServiceError("不明なテンプレートです")
    try:
        _env.from_string(body)
        if key == "item_specifics":
            json.loads(body)
    except Exception as e:  # noqa: BLE001
        raise ServiceError(f"テンプレートの書式エラー: {e}")
    t = get_template(db, key)
    before = t.body
    t.body, t.version, t.updated_by = body, t.version + 1, user.id
    audit.log(db, audit.Actor.user(user), "setting", f"template:{key}", "template.update",
              before={"body": before}, after={"body": body, "version": t.version})
    return t


def context_for(item: Item) -> dict[str, Any]:
    return {
        "cert_number": item.cert_number, "model_id": item.model_id, "card_name_jp": item.card_name_jp or "",
        "card_name_en": item.card_name_en or "", "set_id": item.set_id or "", "set_name": item.set_name or "",
        "card_number": item.card_number or "", "grader": item.grader or "PSA", "grade": item.grade or "",
        "grader_full": GRADER_FULL.get(item.grader or "PSA", item.grader or ""), "year": item.year or "",
        "population": item.psa_population or "",
    }


def render(db: Session, item: Item) -> tuple[str, str, dict[str, str], str]:
    ctx = context_for(item)
    tt, td, ts = get_template(db, "title"), get_template(db, "description"), get_template(db, "item_specifics")
    title = " ".join(_env.from_string(tt.body).render(ctx).split())
    if len(title) > 80:
        title = title[:80].rsplit(" ", 1)[0] if " " in title[:80] else title[:80]
    desc = _env.from_string(td.body).render(ctx)
    raw_specs = json.loads(ts.body)
    specs = {k: " ".join(_env.from_string(str(v)).render(ctx).split()) for k, v in raw_specs.items()}
    specs = {k: v for k, v in specs.items() if v}
    version = f"t{tt.version}.d{td.version}.s{ts.version}"
    return title, desc, specs, version


def image_urls(item: Item) -> list[str]:
    ps = sorted([p for p in item.active_photos() if p.process_status == "done"], key=lambda p: p.seq)
    return [photo_svc.public_url(p) for p in ps]


def current_draft(db: Session, item: Item) -> ListingDraft | None:
    return db.scalar(select(ListingDraft).where(ListingDraft.item_id == item.id,
                                                ListingDraft.status != "superseded")
                     .order_by(ListingDraft.id.desc()).limit(1))


def generate_draft(db: Session, user: User, item: Item) -> ListingDraft:
    if item.status not in (IS.PRICED, IS.READY):
        raise ServiceError("価格確定の在庫のみ出品データを作成できます")
    if pricing.open_quote(db, item) is not None and item.status == IS.PRICED and item.relist_count:
        raise ServiceError("再出品の価格がまだ承認されていません")
    q = pricing.latest_approved(db, item)
    if q is None:
        raise ServiceError("承認済みの価格がありません")
    title, desc, specs, tv = render(db, item)
    defaults = get_setting(db, "listing_defaults")
    for old in db.scalars(select(ListingDraft).where(ListingDraft.item_id == item.id,
                                                     ListingDraft.status.in_(["draft", "ready", "failed"]))):
        old.status = "superseded"
    d = ListingDraft(item_id=item.id, price_quote_id=q.id, title=title, description_html=desc,
                     item_specifics_json=specs, format=q.format,
                     duration_days=int(defaults.get("duration_days", 7)) if q.format == "auction" else 30,
                     start_price=q.approved_start, bin_price=q.approved_bin, currency=q.currency,
                     template_version=tv, status="draft")
    db.add(d)
    db.flush()
    if item.status == IS.READY:
        states.transition_item(db, item, IS.PRICED, audit.Actor.user(user), action="item.draft_regenerated")
    audit.log(db, audit.Actor.user(user), "listing", d.id, "draft.generate",
              after={"item_id": item.id, "title": title, "format": d.format, "start": d.start_price,
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
    if data.get("duration_days"):
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
    if not item.photo("front") or not item.photo("back"):
        raise ServiceError("表・裏の写真がありません")
    pending = [p for p in item.active_photos() if p.process_status != "done"]
    if pending:
        raise ServiceError("画像の整形が終わっていません。少し待ってから確認してください")
    draft.status = "ready"
    draft.confirmed_by, draft.confirmed_at = user.id, utcnow()
    states.transition_item(db, item, IS.READY, audit.Actor.user(user), action="item.ready")
    audit.log(db, audit.Actor.user(user), "listing", draft.id, "draft.confirm", after={"status": "ready"})


def ready_drafts(db: Session) -> list[ListingDraft]:
    return list(db.scalars(select(ListingDraft).join(Item, Item.id == ListingDraft.item_id)
                           .where(Item.status == IS.READY, ListingDraft.status.in_(["ready", "exported", "failed"]))
                           .order_by(ListingDraft.id)).all())

