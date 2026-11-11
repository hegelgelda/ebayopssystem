"""データモデル（要件定義書 8章）。

時刻はすべて naive UTC で保存する。金額は通貨コードと組で持つ。
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON, Boolean, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text,
    UniqueConstraint, text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.timeutil import utcnow


# ---- 状態定義（5章） ----
class ItemStatus:
    INTAKE = "INTAKE"
    PHOTOGRAPHED = "PHOTOGRAPHED"
    PRICED = "PRICED"
    READY = "READY"
    LISTED = "LISTED"
    SOLD = "SOLD"
    WITHDRAWN = "WITHDRAWN"
    ALL = [INTAKE, PHOTOGRAPHED, PRICED, READY, LISTED, SOLD, WITHDRAWN]
    LABELS = {
        INTAKE: "入荷済み", PHOTOGRAPHED: "撮影済み", PRICED: "価格確定", READY: "出品準備完了",
        LISTED: "出品中", SOLD: "売却済み", WITHDRAWN: "取り下げ",
    }


class Category:
    """在庫の区分。PSA は cert 番号、それ以外はツールが振る SKU を在庫のキーにする。"""
    PSA = "psa"
    SINGLE = "single"
    PACK = "pack"
    BOX = "box"
    BULK = "bulk"
    ALL = [PSA, SINGLE, PACK, BOX, BULK]
    LABELS = {PSA: "PSA鑑定品", SINGLE: "シングル", PACK: "パック", BOX: "BOX", BULK: "バルク"}
    SKU_PREFIX = {SINGLE: "S", PACK: "P", BOX: "X", BULK: "L"}
    # 撮影で必須の写真。バルクは全体写真1枚
    # 裏面は任意（全区分で表だけ撮れば価格承認に進める）
    REQUIRED_PHOTOS = {PSA: ["front"], SINGLE: ["front"], PACK: ["front"], BOX: ["front"], BULK: ["front"]}
    PHOTO_LABELS = {BULK: {"front": "全体（必須）", "back": "裏・詳細（任意）"}}


class ListingFormat:
    """出品のレーン。撮影から出品中まで、通常出品とオークションは混ぜずに進める。"""
    FIXED = "fixed"
    AUCTION = "auction"
    ALL = [FIXED, AUCTION]
    LABELS = {FIXED: "通常出品", AUCTION: "オークション"}


class OrderStatus:
    AWAITING_PAYMENT = "AWAITING_PAYMENT"
    AWAITING_SHIPMENT = "AWAITING_SHIPMENT"
    SHIPPED = "SHIPPED"
    EXCEPTION = "EXCEPTION"
    DELIVERED = "DELIVERED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    CASE_OPEN = "CASE_OPEN"
    ALL = [AWAITING_PAYMENT, AWAITING_SHIPMENT, SHIPPED, EXCEPTION, DELIVERED, COMPLETED, CANCELLED, CASE_OPEN]
    OPEN = [AWAITING_PAYMENT, AWAITING_SHIPMENT, SHIPPED, EXCEPTION, DELIVERED, CASE_OPEN]
    LABELS = {
        AWAITING_PAYMENT: "支払待ち", AWAITING_SHIPMENT: "発送待ち", SHIPPED: "発送済み", EXCEPTION: "異常",
        DELIVERED: "配達済み", COMPLETED: "完了", CANCELLED: "キャンセル", CASE_OPEN: "ケース対応中",
    }


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    discord_user_id: Mapped[str] = mapped_column(String(32), unique=True)
    display_name: Mapped[str] = mapped_column(String(100))
    level: Mapped[int] = mapped_column(Integer, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_photographer: Mapped[bool] = mapped_column(Boolean, default=False)
    is_price_approver: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)


class CardCatalog(Base):
    """モデルID → カード名（買取表HTMLの CARDS 定数から取り込む）。"""
    __tablename__ = "card_catalog"
    model_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    set_id: Mapped[str] = mapped_column(String(32))
    card_number: Mapped[str] = mapped_column(String(32))
    name_jp: Mapped[str | None] = mapped_column(String(200))
    name_en: Mapped[str | None] = mapped_column(String(200))
    set_name: Mapped[str | None] = mapped_column(String(200))
    set_name_jp: Mapped[str | None] = mapped_column(String(200))
    rarity: Mapped[str | None] = mapped_column(String(32))
    image_url: Mapped[str | None] = mapped_column(String(300))
    source: Mapped[str | None] = mapped_column(String(32))  # tcg-catalog / csv / html など
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class CertCatalog(Base):
    """自社Courtyardカタログ（cert番号 → モデルID ほか）。"""
    __tablename__ = "cert_catalog"
    cert_number: Mapped[str] = mapped_column(String(32), primary_key=True)
    model_id: Mapped[str | None] = mapped_column(String(64))
    card_name: Mapped[str | None] = mapped_column(String(200))
    set_name: Mapped[str | None] = mapped_column(String(200))
    card_number: Mapped[str | None] = mapped_column(String(32))
    grader: Mapped[str | None] = mapped_column(String(16))
    grade: Mapped[str | None] = mapped_column(String(16))
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


def _default_sku(ctx) -> str:
    """SKU 未指定なら cert 番号（PSA）。それも無ければ一時値（登録処理で区分ごとの SKU に置き換える）。"""
    import uuid
    return ctx.get_current_parameters().get("cert_number") or f"tmp-{uuid.uuid4().hex[:24]}"


class Item(Base):
    __tablename__ = "items"
    # 削除した在庫のIDを再利用しない（SKU は ID から作るので、番号の使い回し・履歴の混同を防ぐ）
    __table_args__ = {"sqlite_autoincrement": True}
    id: Mapped[int] = mapped_column(primary_key=True)
    category: Mapped[str] = mapped_column(String(8), default=Category.PSA, index=True)
    sku: Mapped[str] = mapped_column(String(32), unique=True, default=_default_sku)  # PSA は cert 番号と同じ
    cert_number: Mapped[str | None] = mapped_column(String(32), unique=True)
    model_id: Mapped[str | None] = mapped_column(String(64), index=True)
    listing_format: Mapped[str] = mapped_column(String(8), default=ListingFormat.AUCTION, index=True)
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    quantity_sold: Mapped[int] = mapped_column(Integer, default=0)
    condition: Mapped[str | None] = mapped_column(String(16))  # シングル（未鑑定）の状態
    label_printed_at: Mapped[datetime | None] = mapped_column(DateTime)  # SKUラベルを印刷した日時
    # バルク（まとめ売り）用：枚数・レアリティ（例 AR）・収録セット [{"id": "SV4M", "name": "Future Flash"}, ...]
    card_count: Mapped[int | None] = mapped_column(Integer)
    rarity: Mapped[str | None] = mapped_column(String(16))
    lot_sets: Mapped[list | None] = mapped_column(JSON)
    card_name_jp: Mapped[str | None] = mapped_column(String(200))
    card_name_en: Mapped[str | None] = mapped_column(String(200))
    set_id: Mapped[str | None] = mapped_column(String(32), index=True)
    set_name: Mapped[str | None] = mapped_column(String(200))
    card_number: Mapped[str | None] = mapped_column(String(32))
    grader: Mapped[str] = mapped_column(String(16), default="PSA")
    grade: Mapped[str | None] = mapped_column(String(16))
    psa_population: Mapped[int | None] = mapped_column(Integer)
    year: Mapped[str | None] = mapped_column(String(8))
    info_source: Mapped[str | None] = mapped_column(String(32))  # courtyard / psa / manual
    purchase_price_jpy: Mapped[int | None] = mapped_column(Integer)
    purchase_date: Mapped[date | None] = mapped_column(Date)
    storage_box: Mapped[str | None] = mapped_column(String(32), index=True)
    storage_slot: Mapped[str | None] = mapped_column(String(32))
    memo: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default=ItemStatus.INTAKE, index=True)
    status_changed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    withdraw_reason: Mapped[str | None] = mapped_column(Text)
    relist_count: Mapped[int] = mapped_column(Integer, default=0)
    registered_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    photos: Mapped[list["Photo"]] = relationship(back_populates="item", order_by="Photo.id")
    registrant: Mapped[User | None] = relationship(foreign_keys=[registered_by])

    @property
    def display_name(self) -> str:
        return (self.card_name_jp or self.card_name_en or self.set_name or self.model_id or self.sku or "")

    @property
    def key_label(self) -> str:
        return f"cert {self.cert_number}" if self.category == Category.PSA and self.cert_number else f"SKU {self.sku}"

    @property
    def remaining(self) -> int:
        return max(0, (self.quantity or 1) - (self.quantity_sold or 0))

    @property
    def category_label(self) -> str:
        return Category.LABELS.get(self.category, self.category)

    @property
    def format_label(self) -> str:
        return ListingFormat.LABELS.get(self.listing_format, self.listing_format)

    def required_photos(self) -> list[str]:
        return Category.REQUIRED_PHOTOS.get(self.category, ["front"])

    def active_photos(self) -> list["Photo"]:
        return [p for p in self.photos if p.replaced_by_photo_id is None]

    def photo(self, kind: str) -> "Photo | None":
        for p in self.active_photos():
            if p.kind == kind:
                return p
        return None


class Photo(Base):
    __tablename__ = "photos"
    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), index=True)
    kind: Mapped[str] = mapped_column(String(8))  # front / back / extra
    seq: Mapped[int] = mapped_column(Integer)  # 公開URLの <n>（表=1, 裏=2, 追加=3〜）
    original_path: Mapped[str] = mapped_column(String(500))
    processed_path: Mapped[str | None] = mapped_column(String(500))
    webp_path: Mapped[str | None] = mapped_column(String(500))
    public_token: Mapped[str] = mapped_column(String(64), unique=True)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    process_status: Mapped[str] = mapped_column(String(16), default="pending")  # pending/done/failed
    bg_removed: Mapped[bool] = mapped_column(Boolean, default=False)
    process_error: Mapped[str | None] = mapped_column(Text)
    client_upload_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    uploaded_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    replaced_by_photo_id: Mapped[int | None] = mapped_column(ForeignKey("photos.id"))

    item: Mapped[Item] = relationship(back_populates="photos")

    @property
    def public_rel_path(self) -> str:
        return f"{self.item.sku}/{self.seq}-{self.public_token}.jpg"


class PriceQuote(Base):
    __tablename__ = "price_quotes"
    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), index=True)
    snapshot_json: Mapped[dict] = mapped_column(JSON, default=dict)
    suggested_start: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    suggested_bin: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    proposed_start: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    proposed_bin: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    approved_start: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    approved_bin: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    format: Mapped[str] = mapped_column(String(16), default="auction")
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    rule_version: Mapped[str | None] = mapped_column(String(64))
    proposed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    proposed_at: Mapped[datetime | None] = mapped_column(DateTime)
    proposal_note: Mapped[str | None] = mapped_column(Text)
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)
    self_approved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ListingDraft(Base):
    __tablename__ = "listing_drafts"
    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), index=True)
    price_quote_id: Mapped[int | None] = mapped_column(ForeignKey("price_quotes.id"))
    title: Mapped[str] = mapped_column(String(80))
    description_html: Mapped[str] = mapped_column(Text)
    item_specifics_json: Mapped[dict] = mapped_column(JSON, default=dict)
    format: Mapped[str] = mapped_column(String(16), default="auction")  # auction / fixed
    duration_days: Mapped[int] = mapped_column(Integer, default=7)
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    start_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    bin_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    template_version: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft/ready/exported/published/failed
    export_batch_id: Mapped[str | None] = mapped_column(String(64), index=True)
    error: Mapped[str | None] = mapped_column(Text)
    confirmed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ExportBatch(Base):
    __tablename__ = "export_batches"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    file_path: Mapped[str] = mapped_column(String(500))
    item_count: Mapped[int] = mapped_column(Integer)
    format: Mapped[str | None] = mapped_column(String(8))  # このCSVのレーン（混在させない）
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    result_imported_at: Mapped[datetime | None] = mapped_column(DateTime)


class Listing(Base):
    __tablename__ = "listings"
    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), index=True)
    draft_id: Mapped[int | None] = mapped_column(ForeignKey("listing_drafts.id"))
    ebay_item_id: Mapped[str | None] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(String(80))
    format: Mapped[str] = mapped_column(String(16))
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    start_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    bin_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    current_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    bid_count: Mapped[int | None] = mapped_column(Integer)
    watch_count: Mapped[int | None] = mapped_column(Integer)
    start_time: Mapped[datetime | None] = mapped_column(DateTime)
    end_time: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active / sold / unsold / ended
    source: Mapped[str] = mapped_column(String(8), default="manual")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    item: Mapped[Item] = relationship()

    __table_args__ = (
        # 同じ在庫が同時に2つの出品中listingを持てない（重複出品の防止）
        Index("uq_listings_item_active", "item_id", unique=True, sqlite_where=text("status = 'active'")),
    )


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    ebay_order_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    listing_id: Mapped[int | None] = mapped_column(ForeignKey("listings.id"))
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), index=True)
    buyer_username: Mapped[str | None] = mapped_column(String(100))
    ship_country: Mapped[str | None] = mapped_column(String(2))
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    total_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    fx_rate_jpy: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    fx_source: Mapped[str | None] = mapped_column(String(64))
    gross_profit_jpy: Mapped[int | None] = mapped_column(Integer)
    shipping_cost_jpy: Mapped[int | None] = mapped_column(Integer)  # 実際に払った送料（未入力なら設定の目安を使う）
    status: Mapped[str] = mapped_column(String(20), default=OrderStatus.AWAITING_PAYMENT, index=True)
    status_before_case: Mapped[str | None] = mapped_column(String(20))
    sold_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime)
    ship_by: Mapped[datetime | None] = mapped_column(DateTime)
    payment_due: Mapped[datetime | None] = mapped_column(DateTime)
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime)
    assignee_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    discord_thread_id: Mapped[str | None] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(8), default="manual")
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    item: Mapped[Item] = relationship()
    listing: Mapped[Listing | None] = relationship()
    assignee: Mapped[User | None] = relationship()
    shipments: Mapped[list["Shipment"]] = relationship(back_populates="order", order_by="Shipment.id")

    def active_shipment(self) -> "Shipment | None":
        for s in reversed(self.shipments):
            if not s.deleted:
                return s
        return None


class Shipment(Base):
    __tablename__ = "shipments"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    carrier: Mapped[str] = mapped_column(String(8))  # FEDEX / DHL
    tracking_number: Mapped[str] = mapped_column(String(40))
    entered_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    entered_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    ebay_upload_status: Mapped[str] = mapped_column(String(16), default="pending")  # pending/uploaded/failed
    ebay_uploaded_at: Mapped[datetime | None] = mapped_column(DateTime)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)

    order: Mapped[Order] = relationship(back_populates="shipments")
    events: Mapped[list["TrackingEvent"]] = relationship(order_by="TrackingEvent.occurred_at")


class TrackingEvent(Base):
    __tablename__ = "tracking_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    shipment_id: Mapped[int] = mapped_column(ForeignKey("shipments.id"), index=True)
    normalized_status: Mapped[str] = mapped_column(String(32))
    raw_status: Mapped[str | None] = mapped_column(String(200))
    location: Mapped[str | None] = mapped_column(String(200))
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    entered_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


class ChecklistItem(Base):
    __tablename__ = "checklist_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    item_key: Mapped[str] = mapped_column(String(64))
    checked_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (UniqueConstraint("order_id", "item_key"),)


class Comment(Base):
    __tablename__ = "comments"
    id: Mapped[int] = mapped_column(primary_key=True)
    target_type: Mapped[str] = mapped_column(String(16))
    target_id: Mapped[int] = mapped_column(Integer)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    user: Mapped[User | None] = relationship()
    __table_args__ = (Index("ix_comments_target", "target_type", "target_id"),)


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    target_type: Mapped[str] = mapped_column(String(16))
    target_id: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(48))
    dedupe_key: Mapped[str] = mapped_column(String(64), default="")
    target: Mapped[str] = mapped_column(String(32))  # user:<id> / role:lv2 / none
    channel: Mapped[str] = mapped_column(String(16))
    message: Mapped[str] = mapped_column(Text, default="")
    discord_message_id: Mapped[str | None] = mapped_column(String(32))
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    escalate_at: Mapped[datetime | None] = mapped_column(DateTime)
    acknowledged_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime)
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime)
    __table_args__ = (
        # F4-5：同じ対象の同じイベントは1回だけ
        UniqueConstraint("target_type", "target_id", "event_type", "dedupe_key", name="uq_notifications_event"),
    )


class Outbox(Base):
    """app/worker → bot への投稿依頼（コンテナ間の直接通信を避ける）。"""
    __tablename__ = "outbox"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))  # post / order_thread / sync_roles
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)


class Job(Base):
    """worker が処理する非同期ジョブ（画像処理など）。"""
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    run_after: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class DutySchedule(Base):
    __tablename__ = "duty_schedule"
    weekday: Mapped[int] = mapped_column(Integer, primary_key=True)  # 0=月 … 6=日
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    user: Mapped[User | None] = relationship()


class DutyOverride(Base):
    __tablename__ = "duty_overrides"
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    user: Mapped[User | None] = relationship()


class MarketPrice(Base):
    __tablename__ = "market_prices"
    id: Mapped[int] = mapped_column(primary_key=True)
    model_id: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(32))
    metric: Mapped[str] = mapped_column(String(32), default="value")
    value: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    currency: Mapped[str | None] = mapped_column(String(3))
    count: Mapped[int | None] = mapped_column(Integer)
    as_of: Mapped[date] = mapped_column(Date)
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (UniqueConstraint("model_id", "source", "metric", "as_of", name="uq_market_prices"),)


class FxRate(Base):
    __tablename__ = "fx_rates"
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    pair: Mapped[str] = mapped_column(String(8), primary_key=True)
    rate: Mapped[Decimal] = mapped_column(Numeric(12, 4))
    source: Mapped[str] = mapped_column(String(64))


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[object] = mapped_column(JSON)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Template(Base):
    __tablename__ = "templates"
    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    body: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuditLog(Base):
    """操作ログ。追記のみ（UPDATE/DELETE はDBトリガーで拒否する）。"""
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    actor_type: Mapped[str] = mapped_column(String(8))  # user / system / agent / bot
    actor_id: Mapped[int | None] = mapped_column(Integer)
    target_type: Mapped[str] = mapped_column(String(16), index=True)
    target_id: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64), index=True)
    before_json: Mapped[dict | None] = mapped_column(JSON)
    after_json: Mapped[dict | None] = mapped_column(JSON)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class ApiToken(Base):
    __tablename__ = "api_tokens"
    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    encrypted_access_token: Mapped[str | None] = mapped_column(Text)
    encrypted_refresh_token: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)


class ApiHealth(Base):
    """外部API呼び出しの連続失敗数（3回連続で#systemへ通知）。"""
    __tablename__ = "api_health"
    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime)
    alerted: Mapped[bool] = mapped_column(Boolean, default=False)


class WeeklyReport(Base):
    __tablename__ = "weekly_reports"
    id: Mapped[int] = mapped_column(primary_key=True)
    period_start: Mapped[date] = mapped_column(Date)
    metrics_json: Mapped[dict] = mapped_column(JSON, default=dict)
    llm_output: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String(64))
    tokens: Mapped[int | None] = mapped_column(Integer)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(16), default="pending")
