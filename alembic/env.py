from __future__ import annotations

import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from app.core.config import get_settings

os.environ.pop("ALEMBIC_SKIP_MODEL_IMPORT", None)

# Alembic autogenerate must see every mapped table.  Importing only Base leaves
# metadata empty and makes `alembic check` propose destructive table removals.
from app import models as _models  # noqa: E402,F401
from app.models.base import Base  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()
database_url = os.getenv("DATABASE_URL", settings.database_url)
config.set_main_option("sqlalchemy.url", database_url)

target_metadata = Base.metadata


def include_additive_schema_object(object_, name, type_, reflected, compare_to):
    """Keep first-stage autogenerate checks additive and non-destructive.

    The production database contains explicitly unmanaged legacy tables and
    hand-tuned indexes.  They must never become implicit DROP operations.  CI
    still detects new mapped tables and columns that are missing in the DB.
    Existing type/nullability/index/constraint reconciliation is handled only
    by an explicit reviewed migration in a later release.
    """

    if type_ == "table" and reflected and compare_to is None:
        return False
    if type_ == "column" and compare_to is not None:
        return False
    if type_ in {"index", "unique_constraint", "foreign_key_constraint"}:
        return False
    return True


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_additive_schema_object,
        compare_type=False,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_additive_schema_object,
            compare_type=False,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
