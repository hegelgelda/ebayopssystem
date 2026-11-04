"""PSA以外の区分（シングル・パック・BOX・バルク）、SKU、数量、通常出品／オークションのレーン

Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

CATS = ("psa", "single", "pack", "box", "bulk")


def upgrade() -> None:
    with op.batch_alter_table("items") as b:
        b.add_column(sa.Column("category", sa.String(8), nullable=False, server_default="psa"))
        b.add_column(sa.Column("sku", sa.String(32), nullable=True))
        b.add_column(sa.Column("listing_format", sa.String(8), nullable=False, server_default="auction"))
        b.add_column(sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"))
        b.add_column(sa.Column("quantity_sold", sa.Integer(), nullable=False, server_default="0"))
        b.add_column(sa.Column("condition", sa.String(16), nullable=True))
        b.alter_column("cert_number", existing_type=sa.String(32), nullable=True)
        b.alter_column("model_id", existing_type=sa.String(64), nullable=True)

    # 既存はすべて PSA。SKU = cert 番号。レーンは最後に承認した価格の形式を引き継ぐ
    op.execute("UPDATE items SET sku = cert_number WHERE sku IS NULL")
    op.execute("""
        UPDATE items SET listing_format = COALESCE((
            SELECT q.format FROM price_quotes q WHERE q.item_id = items.id AND q.approved_at IS NOT NULL
            ORDER BY q.approved_at DESC LIMIT 1), 'auction')
    """)
    op.execute("UPDATE items SET quantity_sold = 1 WHERE status = 'SOLD'")

    with op.batch_alter_table("items") as b:
        b.alter_column("sku", existing_type=sa.String(32), nullable=False)
        b.create_unique_constraint("uq_items_sku", ["sku"])
        b.create_index("ix_items_category", ["category"])
        b.create_index("ix_items_listing_format", ["listing_format"])

    for table in ("listing_drafts", "listings", "orders"):
        with op.batch_alter_table(table) as b:
            b.add_column(sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"))
    with op.batch_alter_table("export_batches") as b:
        b.add_column(sa.Column("format", sa.String(8), nullable=True))

    # テンプレートは区分ごとに持つ。既存（PSA用）は psa.* に引き継ぐ
    for key in ("title", "description", "item_specifics"):
        op.execute(f"UPDATE templates SET key = 'psa.{key}' WHERE key = '{key}'")


def downgrade() -> None:
    for key in ("title", "description", "item_specifics"):
        op.execute(f"UPDATE templates SET key = '{key}' WHERE key = 'psa.{key}'")
    op.execute("DELETE FROM templates WHERE key LIKE '%.%'")
    with op.batch_alter_table("export_batches") as b:
        b.drop_column("format")
    for table in ("listing_drafts", "listings", "orders"):
        with op.batch_alter_table(table) as b:
            b.drop_column("quantity")
    with op.batch_alter_table("items") as b:
        b.drop_index("ix_items_listing_format")
        b.drop_index("ix_items_category")
        b.drop_constraint("uq_items_sku", type_="unique")
        for c in ("condition", "quantity_sold", "quantity", "listing_format", "sku", "category"):
            b.drop_column(c)
