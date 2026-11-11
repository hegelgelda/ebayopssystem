"""manual アダプタ（F1-A）：eBay API なしで運用する。

- 出品の投入：Seller Hub の一括出品CSVを生成し、担当者がアップロード。結果CSVを取り込んで商品IDを書き戻す
- 落札・支払：担当者が eBay の画面を見てボタンで記録
- 追跡番号：ツールに入力後、Seller Hub での登録手順を表示。担当者が「eBayに登録済み」を押す
"""
from __future__ import annotations

import csv
import io
import json
import re
import secrets
from datetime import datetime, timedelta
from decimal import Decimal

from jinja2.sandbox import SandboxedEnvironment
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit
from app.adapters.base import ImportResult, ListingAdapter, OrderAdapter, SubmitResult, TrackingUploadResult
from app.config import get_config
from app.models import (  # noqa: F401
    Category, ExportBatch, Item, ItemStatus as IS, Listing, ListingDraft, Order, Shipment, User,
)
from app.services import listing as listing_svc, notify, orders as order_svc, states
from app.services.errors import ServiceError
from app.settings_store import get_setting
from app.timeutil import to_jst, utcnow

_env = SandboxedEnvironment(autoescape=False)


def load_column_config() -> dict:
    return json.loads(get_config().ebay_csv_columns_path.read_text(encoding="utf-8"))


def _norm(h: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (h or "").lower())


class ManualListingAdapter(ListingAdapter):
    name = "manual"

    def build_csv(self, db: Session, drafts: list[ListingDraft]) -> str:
        conf = load_column_config()
        defaults = get_setting(db, "listing_defaults")
        ebay_cats = get_setting(db, "ebay_categories") or {}
        spec_keys: list[str] = []
        for d in drafts:
            for k in (d.item_specifics_json or {}):
                if k not in spec_keys:
                    spec_keys.append(k)
        headers: list[str] = []
        cells: list[tuple[str, object]] = []  # (kind, conf)
        for col in conf["columns"]:
            if col.get("expand") == "item_specifics":
                required = col.get("required", {})  # 例 {"Game": "*C:Game"}（テンプレートで必須の項目は * 付きの列名）
                for k in spec_keys:
                    headers.append(required.get(k) or col.get("header_format", "C:{key}").format(key=k))
                    cells.append(("spec", k))
            else:
                headers.append(col["header"])
                cells.append(("tpl", _env.from_string(col["value"])))
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\r\n")
        for row in conf.get("info_rows", []):
            w.writerow(row)
        w.writerow(headers)
        for d in drafts:
            item = db.get(Item, d.item_id)
            assert item is not None
            psa = item.category == Category.PSA
            cat = (ebay_cats.get(item.category) or {})
            row_defaults = {**defaults, "shipping_profile": listing_svc.shipping_profile_for(defaults, item.category)}
            ctx = {"draft": d, "item": item, "specs": d.item_specifics_json or {},
                   "images": listing_svc.image_urls(item), "defaults": row_defaults,
                   "category_id": cat.get("category_id") or defaults.get("category_id", ""),
                   "condition_id": cat.get("condition_id") or defaults.get("condition_id", ""),
                   "grader_id": conf.get("grader_ids", {}).get((item.grader or "").upper(), item.grader or "") if psa else "",
                   "grade_id": conf.get("grade_ids", {}).get(str(item.grade or ""), item.grade or "") if psa else "",
                   "card_condition_id": (listing_svc.raw_condition(db, item.condition).get("ebay_id", "")
                                         if item.category == Category.SINGLE else "")}
            out = []
            for kind, c in cells:
                if kind == "spec":
                    out.append((d.item_specifics_json or {}).get(c, ""))  # type: ignore[arg-type]
                else:
                    out.append(c.render(ctx).replace("\r\n", "\n").replace("\n", " "))  # type: ignore[union-attr]
            w.writerow(out)
        return buf.getvalue()

    def submit(self, db: Session, user: User, drafts: list[ListingDraft]) -> SubmitResult:
        if not drafts:
            raise ServiceError("出品する在庫を選んでください")
        formats = {d.format for d in drafts}
        if len(formats) > 1:
            raise ServiceError("通常出品とオークションは別々のCSVで出力してください")
        for d in drafts:
            item = db.get(Item, d.item_id)
            if item is None or item.status != IS.READY or d.status not in ("ready", "exported", "failed"):
                raise ServiceError(f"出品準備完了でない在庫が含まれています（下書き {d.id}）")
            if not listing_svc.image_urls(item):
                raise ServiceError(f"{item.key_label} の画像が整形されていません")
        conf = load_column_config()
        text = self.build_csv(db, drafts)
        batch_id = to_jst(utcnow()).strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)  # type: ignore[union-attr]
        path = get_config().exports_dir / f"ebay-upload-{batch_id}.csv"
        path.write_text(text, encoding=conf.get("encoding", "utf-8-sig"))
        db.add(ExportBatch(id=batch_id, file_path=str(path), item_count=len(drafts), format=formats.pop(),
                           created_by=user.id))
        for d in drafts:
            d.status = "exported"
            d.export_batch_id = batch_id
            d.error = None
        audit.log(db, audit.Actor.user(user), "listing", batch_id, "listing.export_csv",
                  after={"draft_ids": [d.id for d in drafts], "file": path.name})
        return SubmitResult(batch_id=batch_id, file_path=str(path))

    def import_result(self, db: Session, user: User, data: bytes) -> ImportResult:
        conf = load_column_config().get("result_columns", {})
        text = data.decode("utf-8-sig", errors="replace")
        rows = list(csv.reader(io.StringIO(text)))
        res = ImportResult()
        # ヘッダー行を探す（先頭に説明行がある場合に備える）
        header_idx = None
        cols: dict[str, int] = {}
        for i, row in enumerate(rows[:20]):
            normed = [_norm(c) for c in row]
            found = {}
            for key, names in conf.items():
                if key.startswith("_"):
                    continue
                for n in names:
                    if n in normed:
                        found[key] = normed.index(n)
                        break
            if "custom_label" in found and "item_id" in found:
                header_idx, cols = i, found
                break
        if header_idx is None:
            raise ServiceError("結果CSVに CustomLabel（SKU）と ItemID の列が見つかりません")
        for row in rows[header_idx + 1:]:
            if not any(row):
                continue

            def get(k):
                i = cols.get(k)
                return row[i].strip() if i is not None and i < len(row) else ""
            key, ebay_id, status, err = get("custom_label"), get("item_id"), get("status"), get("error")
            if not key:
                continue
            item = db.scalar(select(Item).where(Item.sku == key)) or \
                db.scalar(select(Item).where(Item.cert_number == key))
            if item is None:
                res.skipped.append((key, "在庫が見つかりません"))
                continue
            draft = db.scalar(select(ListingDraft).where(ListingDraft.item_id == item.id,
                                                         ListingDraft.status.in_(["exported", "ready", "failed"]))
                              .order_by(ListingDraft.id.desc()).limit(1))
            ok = bool(re.fullmatch(r"\d{9,15}", ebay_id or "")) and not re.search(r"fail|error", status, re.I)
            if ok:
                if item.status != IS.READY or draft is None:
                    res.skipped.append((key, f"出品準備完了ではありません（{IS.LABELS.get(item.status)}）"))
                    continue
                mark_listed(db, user, draft, ebay_id)
                res.listed.append((key, ebay_id))
            else:
                msg = err or status or "商品IDが返されませんでした"
                if draft is not None:
                    draft.status = "failed"
                    draft.error = msg
                res.failed.append((key, msg))
                audit.log(db, audit.Actor.user(user), "item", item.id, "listing.failed", after={"error": msg})
                notify.notify(db, "listing_failed", "item", item.id,
                              f"❗ 出品投入の失敗：{item.key_label} {item.display_name} / {msg}",
                              channel="listing", dedupe_key=str(draft.id if draft else 0),
                              link_path="/listings")
        audit.log(db, audit.Actor.user(user), "listing", None, "listing.import_result",
                  after={"listed": res.listed, "failed": res.failed, "skipped": res.skipped})
        if res.listed or res.failed:
            notify.enqueue_post(db, "listing",
                                f"📤 出品結果の取り込み：成功 {len(res.listed)}件 / 失敗 {len(res.failed)}件")
        return res


def mark_listed(db: Session, user: User, draft: ListingDraft, ebay_item_id: str,
                start_time: datetime | None = None) -> Listing:
    ebay_item_id = (ebay_item_id or "").strip()
    if not re.fullmatch(r"\d{9,15}", ebay_item_id):
        raise ServiceError("eBayの商品IDは数字で入力してください")
    item = db.get(Item, draft.item_id)
    assert item is not None
    if db.scalar(select(Listing).where(Listing.item_id == item.id, Listing.status == "active")):
        raise ServiceError("この在庫は既に出品中です（重複出品の防止）")
    start = start_time or utcnow()
    lst = Listing(item_id=item.id, draft_id=draft.id, ebay_item_id=ebay_item_id, title=draft.title,
                  format=draft.format, quantity=draft.quantity, start_price=draft.start_price,
                  bin_price=draft.bin_price,
                  current_price=draft.start_price, currency=draft.currency, start_time=start,
                  end_time=start + timedelta(days=draft.duration_days) if draft.format == "auction" else None,
                  status="active", source="manual")
    db.add(lst)
    draft.status = "published"
    draft.error = None
    db.flush()
    states.transition_item(db, item, IS.LISTED, audit.Actor.user(user), action="item.listed")
    audit.log(db, audit.Actor.user(user), "listing", lst.id, "listing.create",
              after={"item_id": item.id, "ebay_item_id": ebay_item_id, "end_time": lst.end_time})
    notify.resolve(db, "item", item.id, ["listing_failed"])
    return lst


class ManualOrderAdapter(OrderAdapter):
    name = "manual"

    def record_sale(self, db, user, listing, *, buyer, amount, currency, ship_country, sold_at=None,
                    ebay_order_id=None, paid=False, quantity=1) -> Order:
        o = order_svc.create_order_from_sale(db, user, listing, buyer=buyer,
                                             amount=Decimal(str(amount)) if amount is not None else None,
                                             currency=currency, ship_country=ship_country, sold_at=sold_at,
                                             ebay_order_id=ebay_order_id, source="manual", quantity=quantity)
        if paid:
            order_svc.record_payment(db, user, o, sold_at, None)
        return o

    def record_payment(self, db, user, order, paid_at=None, version=None) -> None:
        order_svc.record_payment(db, user, order, paid_at, version)

    def upload_tracking(self, db: Session, user: User, shipment: Shipment) -> TrackingUploadResult:
        o = shipment.order
        carrier = order_svc.CARRIERS.get(shipment.carrier, shipment.carrier)
        item_id = o.listing.ebay_item_id if o.listing else ""
        find = [
            "eBay Seller Hub を開き、「Orders（注文）」→「Awaiting shipment（発送待ち）」を表示する",
            f"商品ID {item_id}（落札者 {o.buyer_username or '-'}）の注文を探す",
        ]
        done = "注文が「Shipped（発送済み）」になったことを確認し、このページの「eBayに登録済み」を押す"
        if shipment.carrier == "NONE":
            return TrackingUploadResult(status="pending", instructions=[
                *find, "「Mark as shipped（発送済みにする）」を押す（追跡番号は入れない）", done])
        if shipment.carrier == "SPEEDPAK":
            return TrackingUploadResult(status="pending", instructions=[
                *find, "SpeedPAK のラベルを eBay から作った場合、追跡番号は自動で反映されます。反映されていなければ"
                       f"「Add tracking number」で「eBay SpeedPAK」を選び「{shipment.tracking_number}」を入力", done])
        eb_carrier = {"JPPOST": "Japan Post"}.get(shipment.carrier, carrier)
        return TrackingUploadResult(status="pending", instructions=[
            *find, "「Add tracking number（追跡番号を追加）」を押す",
            f"Carrier（配送業者）に「{eb_carrier}」を選び、追跡番号「{shipment.tracking_number}」を入力して保存する",
            done])
