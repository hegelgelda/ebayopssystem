"""説明文テンプレートの配送・返品・支払の文面を Business Policies v1.1 に合わせる

一度も編集されていない（version = 1）説明文テンプレートだけを新しい初期値に置き換える。
編集済みのテンプレートには手を付けない（設定画面で手動で直す）。

Revision ID: 0005
Revises: 0004
"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from app.services.listing import DEFAULT_TEMPLATES
    templates = sa.table("templates", sa.column("key", sa.String), sa.column("body", sa.Text),
                         sa.column("version", sa.Integer))
    for cat, parts in DEFAULT_TEMPLATES.items():
        op.execute(templates.update()
                   .where(templates.c.key == f"{cat}.description", templates.c.version == 1)
                   .values(body=parts["description"]))


def downgrade() -> None:
    pass
