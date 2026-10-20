# Spec: 03 §6（物理设计）。环境变量 BELEGT_DB_URL 覆盖 alembic.ini 的 sqlalchemy.url。
from __future__ import annotations

import os

from alembic import context
from sqlalchemy import engine_from_config, pool

from belegt_core.store.tables import metadata

config = context.config
if os.environ.get("BELEGT_DB_URL"):
    config.set_main_option("sqlalchemy.url", os.environ["BELEGT_DB_URL"])
target_metadata = metadata


def run_migrations_offline() -> None:
    context.configure(url=config.get_main_option("sqlalchemy.url"), target_metadata=target_metadata,
                      literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.",
                                     poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
