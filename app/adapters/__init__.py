"""settings.ebay_adapter で manual / api を切り替える。Phase 1 は manual のみ。"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.adapters.base import ListingAdapter, OrderAdapter
from app.adapters.manual import ManualListingAdapter, ManualOrderAdapter
from app.settings_store import get_setting


def get_listing_adapter(db: Session) -> ListingAdapter:
    name = get_setting(db, "ebay_adapter")
    if name == "api":
        raise NotImplementedError("api アダプタは Phase 3 で実装します")
    return ManualListingAdapter()


def get_order_adapter(db: Session) -> OrderAdapter:
    name = get_setting(db, "ebay_adapter")
    if name == "api":
        raise NotImplementedError("api アダプタは Phase 3 で実装します")
    return ManualOrderAdapter()
