from alembic import context

from app import models  # noqa: F401  モデルを登録する
from app.db import Base, get_engine

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    from app.config import get_config
    context.configure(url=get_config().database_url, target_metadata=target_metadata, literal_binds=True,
                      render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = get_engine()
    with engine.connect() as connection:
        # SQLite の batch 変更はテーブルを作り直す（DROP → RENAME）ため、参照されているテーブル（items 等）を
        # 外部キー制約が有効なまま作り直すと失敗する。マイグレーション中だけ無効にし、終わったら整合性を確認する。
        # （PRAGMA foreign_keys はトランザクション内では効かないので、開始前に実行する）
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.commit()  # 上の実行で始まった暗黙のトランザクションを閉じ、Alembic に自前で管理させる
        context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()
        problems = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.commit()
        if problems:
            raise RuntimeError(f"マイグレーション後に外部キーの不整合があります: {problems[:10]}")


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
