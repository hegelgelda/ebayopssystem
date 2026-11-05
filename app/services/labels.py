"""SKUラベル（QRコード付き）の印刷。

PSA 以外の在庫には読み取れる番号が無いので、SKU を QR にしたラベルを貼っておき、
撮影・ピッキング・発送のときに読み取って在庫を開く。QR の中身は SKU の文字列だけ（例 S-00012）。
"""
from __future__ import annotations

import io

import segno
from markupsafe import Markup
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit
from app.models import Category, Item, ItemStatus as IS, User
from app.timeutil import utcnow


def qr_svg(text: str) -> Markup:
    """印刷用の QR（SVG）。サイズは CSS で指定するので viewBox だけ持たせる。"""
    # 短い文字列だと segno は Micro QR を選ぶが、スマホやブラウザの読み取りは Micro QR に対応しないので通常の QR に固定
    q = segno.make(text, error="m", micro=False)
    buf = io.BytesIO()
    q.save(buf, kind="svg", scale=1, border=1, xmldecl=False, svgns=True, omitsize=True, dark="#000")
    return Markup(buf.getvalue().decode("utf-8"))


def pending_items(db: Session, include_psa: bool = False) -> list[Item]:
    """ラベル未印刷の在庫（売却済み・取り下げを除く）。PSA は cert のバーコードがあるので既定では除く。"""
    stmt = select(Item).where(Item.label_printed_at.is_(None), Item.status.not_in([IS.SOLD, IS.WITHDRAWN]))
    if not include_psa:
        stmt = stmt.where(Item.category != Category.PSA)
    return list(db.scalars(stmt.order_by(Item.id)).all())


def mark_printed(db: Session, user: User, items: list[Item]) -> int:
    now = utcnow()
    for it in items:
        it.label_printed_at = now
    if items:
        audit.log(db, audit.Actor.user(user), "item", None, "label.printed",
                  after={"items": [it.sku for it in items]})
    return len(items)
