"""eBay との接点（出品の投入、落札・支払・発送の検知）を切り出したアダプタ（F1）。

manual：CSV出力・結果CSV取り込み・担当者のボタン操作で記録する（Phase 1）。
api   ：eBay Sell Inventory / Fulfillment API（Phase 3 で実装）。
それ以外の機能はどちらでも同じコードで動く。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models import ListingDraft, Listing, Order, Shipment, User


@dataclass
class SubmitResult:
    batch_id: str | None = None
    file_path: str | None = None  # manual：生成したCSV
    published: list[int] = field(default_factory=list)  # draft id
    failed: dict[int, str] = field(default_factory=dict)


@dataclass
class ImportResult:
    listed: list[tuple[str, str]] = field(default_factory=list)  # (SKU, ebay_item_id)
    failed: list[tuple[str, str]] = field(default_factory=list)  # (SKU, error)
    skipped: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class TrackingUploadResult:
    status: str  # uploaded / pending / failed
    instructions: list[str] = field(default_factory=list)
    error: str | None = None


class ListingAdapter(ABC):
    name: str

    @abstractmethod
    def submit(self, db: Session, user: User, drafts: list[ListingDraft]) -> SubmitResult: ...

    def import_result(self, db: Session, user: User, data: bytes) -> ImportResult:
        raise NotImplementedError


class OrderAdapter(ABC):
    name: str

    @abstractmethod
    def record_sale(self, db: Session, user: User, listing: Listing, *, buyer: str, amount: Decimal,
                    currency: str, ship_country: str, sold_at: datetime | None = None,
                    ebay_order_id: str | None = None, paid: bool = False, quantity: int = 1) -> Order: ...

    @abstractmethod
    def record_payment(self, db: Session, user: User, order: Order, paid_at: datetime | None = None,
                       version: int | None = None) -> None: ...

    @abstractmethod
    def upload_tracking(self, db: Session, user: User, shipment: Shipment) -> TrackingUploadResult: ...

    def poll(self, db: Session) -> dict[str, Any]:
        """api アダプタで注文・出品状況を取りに行く。manual では何もしない。"""
        return {}
