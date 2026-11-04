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
        context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
