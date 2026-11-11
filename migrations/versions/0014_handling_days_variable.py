"""説明文の「within N business days」を設定（発送までの営業日数）から入れる

全テンプレート（編集済みも含む）の「within N business days」を {{ handling_days }} に置き換える。
編集で 2 以外の日数（例：3）にしていた場合は、その日数を設定 ship_business_days に引き継ぐ。

Revision ID: 0014
Revises: 0013
"""
import json
import re

from alembic import op
import sqlalchemy as sa

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

_RX = re.compile(r"within (\d+) business days")


def upgrade() -> None:
    conn = op.get_bind()
    found: set[int] = set()
    for key, body in conn.execute(sa.text("SELECT key, body FROM templates")).fetchall():
        nums = {int(n) for n in _RX.findall(body or "")}
        if not nums:
            continue
        found |= nums
        new = _RX.sub("within {{ handling_days }} business days", body)
        conn.execute(sa.text("UPDATE templates SET body = :b WHERE key = :k"), {"b": new, "k": key})
    custom = found - {2}
    if len(custom) == 1:
        days = custom.pop()
        row = conn.execute(sa.text("SELECT 1 FROM settings WHERE key = 'ship_business_days'")).fetchone()
        if row:
            conn.execute(sa.text("UPDATE settings SET value = :v, updated_at = CURRENT_TIMESTAMP WHERE key = 'ship_business_days'"),
                         {"v": json.dumps(days)})
        else:
            conn.execute(sa.text("INSERT INTO settings (key, value, updated_at) VALUES ('ship_business_days', :v, CURRENT_TIMESTAMP)"),
                         {"v": json.dumps(days)})


def downgrade() -> None:
    pass
