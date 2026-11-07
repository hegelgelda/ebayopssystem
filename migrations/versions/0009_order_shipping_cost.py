"""注文ごとの実際の送料（円）

Revision ID: 0009
Revises: 0008
"""
from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("orders") as b:
        b.add_column(sa.Column("shipping_cost_jpy", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("orders") as b:
        b.drop_column("shipping_cost_jpy")
