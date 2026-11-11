"""items の ID を再利用しない（AUTOINCREMENT）。在庫の削除で SKU 番号が使い回されないようにする

Revision ID: 0010
Revises: 0009
"""
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("items", recreate="always", table_kwargs={"sqlite_autoincrement": True}):
        pass


def downgrade() -> None:
    with op.batch_alter_table("items", recreate="always", table_kwargs={"sqlite_autoincrement": False}):
        pass
