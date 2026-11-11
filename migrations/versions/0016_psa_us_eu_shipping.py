"""PSA もシングル・パック・バルクと同じ配送（Torelis Single US + EU）に

- 一度も編集されていない（version = 1）PSA の説明文を新しい初期値に置き換える
- 設定 listing_defaults を保存済みで、区分ごとの配送ポリシー名に PSA が無ければ追加する

Revision ID: 0016
Revises: 0015
"""
import json

from alembic import op
import sqlalchemy as sa

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from app.services.listing import DEFAULT_TEMPLATES
    templates = sa.table("templates", sa.column("key", sa.String), sa.column("body", sa.Text),
                         sa.column("version", sa.Integer))
    op.execute(templates.update()
               .where(templates.c.key == "psa.description", templates.c.version == 1)
               .values(body=DEFAULT_TEMPLATES["psa"]["description"]))
    conn = op.get_bind()
    row = conn.execute(sa.text("SELECT value FROM settings WHERE key = 'listing_defaults'")).fetchone()
    if row and row[0]:
        val = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        by = val.get("shipping_profile_by_category")
        if isinstance(by, dict) and "psa" not in by:
            by["psa"] = by.get("single") or "Torelis Single US + EU"
            conn.execute(sa.text("UPDATE settings SET value = :v WHERE key = 'listing_defaults'"),
                         {"v": json.dumps(val, ensure_ascii=False)})


def downgrade() -> None:
    pass
