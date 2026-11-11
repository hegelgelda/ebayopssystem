"""パックの説明文をシングルと同じ配送（米国・欧州：エコノミー＋SpeedPAK、関税込み）に

一度も編集されていない（version = 1）パックの説明文だけを新しい初期値に置き換える。

Revision ID: 0013
Revises: 0012
"""
from alembic import op
import sqlalchemy as sa

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from app.services.listing import DEFAULT_TEMPLATES
    templates = sa.table("templates", sa.column("key", sa.String), sa.column("body", sa.Text),
                         sa.column("version", sa.Integer))
    op.execute(templates.update()
               .where(templates.c.key == "pack.description", templates.c.version == 1)
               .values(body=DEFAULT_TEMPLATES["pack"]["description"]))


def downgrade() -> None:
    pass
