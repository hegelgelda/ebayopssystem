"""Item Specifics テンプレートを eBay のカテゴリテンプレート（2026-11-07 確認）に合わせる

Graded / Grade / Professional Grader / Certification Number は Condition Descriptor（CD: 列）で渡すので
Item Specifics から外す。一度も編集されていない（version = 1）テンプレートだけを置き換える。

Revision ID: 0006
Revises: 0005
"""
from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
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
