"""画面から変更できる運用設定（settings テーブル）。未設定のキーは DEFAULTS を使う。"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from sqlalchemy.orm import Session

from app.models import Setting

DEFAULTS: dict[str, Any] = {
    # eBayアダプタ（Phase 1 は manual のみ）
    "ebay_adapter": "manual",
    # 撮影した写真の背景除去（切り出し）。オフなら撮った写真をそのまま使う（向きの補正と長辺1600pxへの縮小のみ）
    "remove_background": False,
    # 出品日（0=月 … 6=日）。初期値は火・金
    "listing_weekdays": [1, 4],
    # 祝日に曜日担当を優先するか（F5-3。初期値は祝日＝不在）
    "duty_on_holiday": False,
    # 仕入値を登録者以外の Lv1 に見せるか
    "show_purchase_price_to_lv1": False,
    # エスカレーション時間（分）。None はエスカレーションなし
    "escalation_minutes": {
        "listing_failed": 120,
        "order_sold": 120,
        "auction_unrecorded": 240,
        "payment_reminder": None,  # 期限超過時点でLv2へ（別ロジック）
        "ship_due_today": None,  # 12:00 JST 時点で未発送ならLv2へ（別ロジック）
        "tracking_not_uploaded": 60,
        "tracking_upload_failed": 60,
        "transit_exception": 240,
        "case_opened": 120,
    },
    "ship_due_escalate_hour_jst": 12,
    "ship_due_notify_hour_jst": 9,
    "digest_times_jst": ["09:00", "17:00"],
    "stale_days": {"INTAKE": 7, "PHOTOGRAPHED": 3},
    "checklist_items": [
        {"key": "picked", "label": "ピッキング済み"},
        {"key": "packed", "label": "梱包済み"},
        {"key": "pack_photo", "label": "梱包写真の撮影済み"},
        {"key": "invoice", "label": "インボイス作成済み"},
        {"key": "label", "label": "ラベル作成済み"},
        {"key": "pickup", "label": "集荷手配済み"},
    ],
    "tracking_regex": {
        "FEDEX": r"^(\d{12}|\d{15}|\d{20}|\d{22})$",
        "DHL": r"^\d{10}$",
        "JPPOST": r"^[A-Z]{2}\d{9}JP$",
        "SPEEDPAK": r"^[A-Z0-9]{10,34}$",
    },
    # 価格ルール（F0-3）
    "price_rule": {
        "start": "breakeven",
        "bin_coefficient": 1.2,
        "bin_sources": ["pricecharting_psa10", "courtyard_p2p"],
        "relist_discount": 0.05,
        "round_to": 1,
        "default_format": "auction",
    },
    # 損益分岐価格の計算に使う料率・費用
    "fees": {
        "ebay_final_value_rate": 0.1325,
        "international_fee_rate": 0.0165,
        "per_order_fee_usd": 0.40,
        "duty_rate": 0.10,
        "shipping_cost_jpy": 4000,
        "fx_fallback_usdjpy": 150.0,
    },
    # 出品データの固定値（F0-4）
    "listing_defaults": {
        "category_id": "183454",
        "condition_id": "2750",
        "duration_days": 7,
        "shipping_profile": "Torelis Free Express (DDP)",
        # 区分ごとの配送ポリシー名（無ければ shipping_profile）。シングルは米国・欧州向け（エコノミー＋SpeedPAK）
        "shipping_profile_by_category": {"single": "Torelis Single US + EU"},
        "payment_profile": "Immediate Payment",
        "return_profile": "No Returns",
        "location": "Tokyo",
        "country": "JP",
        "postal_code": "",
        "site": "US",
        "currency": "USD",
        "dispatch_time_max": 2,
    },
    # 区分ごとの既定レーン（入荷画面で変更できる）
    "default_format_by_category": {"psa": "auction", "single": "fixed", "pack": "fixed", "box": "fixed",
                                   "bulk": "auction"},
    # 区分ごとの eBay カテゴリIDとコンディションID（2026-11-07 Seller Hub のテンプレートで確認）
    #   183454 CCG Individual Cards（2750 Graded / 4000 Ungraded）、183456 Sealed Packs、261044 Sealed Boxes、
    #   183455 CCG Mixed Card Lots。パック・BOX の 1000（New）とバルクの 3000（Used）はテンプレートに記載が無く要確認
    "ebay_categories": {
        "psa": {"category_id": "183454", "condition_id": "2750"},
        "single": {"category_id": "183454", "condition_id": "4000"},
        "pack": {"category_id": "183456", "condition_id": "1000"},
        "box": {"category_id": "261044", "condition_id": "1000"},
        "bulk": {"category_id": "183455", "condition_id": "3000"},
    },
    # シングル（未鑑定）の状態。ebay_id は Condition Descriptor「Card Condition（40001）」の値（2026-11-07 テンプレートで確認）
    "raw_conditions": [
        {"key": "nm", "label": "Near Mint or Better", "short": "NM", "ebay_id": "400010"},
        {"key": "ex", "label": "Lightly Played (Excellent)", "short": "LP", "ebay_id": "400015"},
        {"key": "vg", "label": "Moderately Played (Very Good)", "short": "MP", "ebay_id": "400016"},
        {"key": "poor", "label": "Heavily Played (Poor)", "short": "HP", "ebay_id": "400017"},
    ],
    # シングルの型番検索に使うカード一覧の取得元（毎日自動で更新。設定画面から手動更新も可）
    "card_catalog_url": "https://hegelgelda.github.io/tcg-catalog/",
    # tcg-catalog に無い新弾・英語名を Limitless TCG（日本語版）から補う。弾名の誤りもここで直す
    "card_catalog_limitless": True,
    "payment_due_days": 4,
    "ship_business_days": 2,
    "delivered_to_completed_hours": 48,
}


def get_setting(db: Session, key: str) -> Any:
    row = db.get(Setting, key)
    if row is None:
        return copy.deepcopy(DEFAULTS.get(key))
    default = DEFAULTS.get(key)
    if isinstance(default, dict) and isinstance(row.value, dict):
        merged = copy.deepcopy(default)
        merged.update(row.value)
        return merged
    return row.value


def set_setting(db: Session, key: str, value: Any, user_id: int | None) -> tuple[Any, Any]:
    before = get_setting(db, key)
    row = db.get(Setting, key)
    if row is None:
        row = Setting(key=key, value=value, updated_by=user_id)
        db.add(row)
    else:
        row.value = value
        row.updated_by = user_id
    db.flush()
    return before, value


def rule_version(db: Session) -> str:
    payload = json.dumps({"price_rule": get_setting(db, "price_rule"), "fees": get_setting(db, "fees")},
                         sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:12]
