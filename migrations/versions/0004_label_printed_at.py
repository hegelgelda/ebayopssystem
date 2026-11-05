"""SKUラベルの印刷日時

Revision ID: 0004
Revises: 0003
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("items") as b:
        b.add_column(sa.Column("label_printed_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("items") as b:
        b.drop_column("label_printed_at")
