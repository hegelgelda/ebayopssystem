"""パック・BOX・バルクの Item Specifics を eBay のテンプレート（Sealed Packs / Sealed Boxes / Mixed Card Lots）に合わせる

Configuration の値を eBay の候補（Pack / Box）にし、Number of Packs / Number of Boxes を追加する。
一度も編集されていない（version = 1）テンプレートだけを置き換える。

Revision ID: 0007
Revises: 0006
"""
from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from app.services.listing import DEFAULT_TEMPLATES
    templates = sa.table("templates", sa.column("key", sa.String), sa.column("body", sa.Text),
                         sa.column("version", sa.Integer))
    for cat, parts in DEFAULT_TEMPLATES.items():
        op.execute(templates.update()
                   .where(templates.c.key == f"{cat}.item_specifics", templates.c.version == 1)
                   .values(body=parts["item_specifics"]))


def downgrade() -> None:
    pass
