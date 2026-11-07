"""バルク（まとめ売り）用の枚数・レアリティ・収録セット。バルクのテンプレートを AR まとめ売り向けにする

一度も編集されていない（version = 1）バルクのテンプレートだけを新しい初期値に置き換える。

Revision ID: 0008
Revises: 0007
"""
from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("items") as b:
        b.add_column(sa.Column("card_count", sa.Integer(), nullable=True))
        b.add_column(sa.Column("rarity", sa.String(16), nullable=True))
        b.add_column(sa.Column("lot_sets", sa.JSON(), nullable=True))
    from app.services.listing import DEFAULT_TEMPLATES
    templates = sa.table("templates", sa.column("key", sa.String), sa.column("body", sa.Text),
                         sa.column("version", sa.Integer))
    for part, body in DEFAULT_TEMPLATES["bulk"].items():
        op.execute(templates.update()
                   .where(templates.c.key == f"bulk.{part}", templates.c.version == 1)
                   .values(body=body))


def downgrade() -> None:
    with op.batch_alter_table("items") as b:
        for c in ("lot_sets", "rarity", "card_count"):
            b.drop_column(c)
