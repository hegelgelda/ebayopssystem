"""カード一覧に日本語セット名・レアリティ・画像URL・取得元を追加（tcg-catalog の取り込み用）

Revision ID: 0003
Revises: 0002
"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("card_catalog") as b:
        b.add_column(sa.Column("set_name_jp", sa.String(200), nullable=True))
        b.add_column(sa.Column("rarity", sa.String(32), nullable=True))
        b.add_column(sa.Column("image_url", sa.String(300), nullable=True))
        b.add_column(sa.Column("source", sa.String(32), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("card_catalog") as b:
        for c in ("source", "image_url", "rarity", "set_name_jp"):
            b.drop_column(c)
