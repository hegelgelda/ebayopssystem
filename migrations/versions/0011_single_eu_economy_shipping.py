"""シングルの説明文を「欧州向け：エコノミー無料・スタンダード$5・SpeedPAK Express $23」に

一度も編集されていない（version = 1）シングルの説明文だけを新しい初期値に置き換える。

Revision ID: 0011
Revises: 0010
"""
from alembic import op
import sqlalchemy as sa

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from app.services.listing import DEFAULT_TEMPLATES
    templates = sa.table("templates", sa.column("key", sa.String), sa.column("body", sa.Text),
                         sa.column("version", sa.Integer))
    op.execute(templates.update()
               .where(templates.c.key == "single.description", templates.c.version == 1)
               .values(body=DEFAULT_TEMPLATES["single"]["description"]))


def downgrade() -> None:
    pass
