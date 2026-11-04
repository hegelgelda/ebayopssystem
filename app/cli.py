"""管理用コマンド。

  python -m app.cli migrate
  python -m app.cli create-admin --discord-id 123456789012345678 --name 管理者
  python -m app.cli import-market --file prices.csv                         # 縦持ちCSV
  python -m app.cli import-market-mapped --config config/market_import.json # 横持ちCSV（列マッピング）
  python -m app.cli import-cert-catalog --file courtyard_catalog.csv
  python -m app.cli import-cards --file buyback.html                        # CARDS 定数
  python -m app.cli import-catalog [--url https://hegelgelda.github.io/tcg-catalog/]  # カード一覧をWebから更新
  python -m app.cli fetch-fx
  python -m app.cli backup
  python -m app.cli list-backups
  python -m app.cli restore --stamp 20261104-030000
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.config import get_config


def migrate() -> None:
    from alembic import command
    from alembic.config import Config as AlembicConfig

    root = Path(__file__).resolve().parent.parent
    cfg = AlembicConfig(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    get_config().ensure_dirs()
    command.upgrade(cfg, "head")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="app.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate")
    a = sub.add_parser("create-admin", help="最初のLv2ユーザーを作る")
    a.add_argument("--discord-id", required=True)
    a.add_argument("--name", required=True)
    a.add_argument("--level", type=int, default=2)
    for name in ("import-market", "import-cert-catalog", "import-cards"):
        x = sub.add_parser(name)
        x.add_argument("--file", required=True)
    ic = sub.add_parser("import-catalog", help="tcg-catalog からカード一覧（型番・名前）を取り込む")
    ic.add_argument("--url", default="")
    m = sub.add_parser("import-market-mapped")
    m.add_argument("--config", required=True)
    sub.add_parser("fetch-fx")
    sub.add_parser("backup")
    sub.add_parser("list-backups")
    r = sub.add_parser("restore")
    r.add_argument("--stamp", required=True)
    args = p.parse_args(argv)

    if args.cmd == "migrate":
        migrate()
        print("migrated")
        return 0
    if args.cmd == "restore":
        from app.services import backup
        backup.restore(args.stamp)
        print("restored. コンテナを再起動してください: docker compose restart")
        return 0
    if args.cmd == "list-backups":
        from app.services import backup
        print("\n".join(backup.list_backups()))
        return 0
    if args.cmd == "backup":
        from app.services import backup
        print(json.dumps(backup.create_backup(), ensure_ascii=False))
        return 0

    from app.db import session_scope
    from app.services import importers
    with session_scope() as db:
        if args.cmd == "create-admin":
            from app.services import users
            u = users.create_user(db, args.discord_id, args.name, args.level, actor_user=None)
            print(f"created user id={u.id} level={u.level}")
        elif args.cmd == "import-market":
            ins, upd = importers.import_market_long(db, Path(args.file))
            print(f"market_prices: inserted={ins} updated={upd}")
        elif args.cmd == "import-market-mapped":
            conf = json.loads(Path(args.config).read_text(encoding="utf-8"))
            for f in conf["files"]:
                ins, upd = importers.import_market_mapped(db, Path(f["path"]), f)
                print(f"{f['path']}: inserted={ins} updated={upd}")
        elif args.cmd == "import-cert-catalog":
            print(f"cert_catalog: {importers.import_cert_catalog(db, Path(args.file))} rows")
        elif args.cmd == "import-catalog":
            from app.settings_store import get_setting
            url = args.url or get_setting(db, "card_catalog_url")
            print(f"card_catalog from {url}: {importers.import_tcg_catalog(db, url)}")
        elif args.cmd == "import-cards":
            print(f"card_catalog: {importers.import_cards(db, Path(args.file))} rows")
        elif args.cmd == "fetch-fx":
            from app.services import fx
            row = fx.fetch_usdjpy(db)
            print(f"USDJPY {row.date} {row.rate}" if row else "failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
