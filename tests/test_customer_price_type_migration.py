from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect


def _load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/b9e5d7f3a012_add_customer_price_type_core.py"
    )
    spec = importlib.util.spec_from_file_location("customer_price_type_migration", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_migration_upgrade_and_downgrade_with_circular_foreign_keys(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    migration = _load_migration()
    try:
        with engine.begin() as connection:
            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()

            inspector = inspect(connection)
            expected = {
                "customer_price_type_profile",
                "customer_price_type_run",
                "customer_price_type_snapshot",
                "customer_price_type_case",
                "customer_price_type_case_event",
            }
            assert expected <= set(inspector.get_table_names())
            profile_fks = {
                item["name"] for item in inspector.get_foreign_keys("customer_price_type_profile")
            }
            assert profile_fks == {
                "fk_customer_price_type_profile_latest_snapshot",
                "fk_customer_price_type_profile_open_case",
            }

            migration.downgrade()
            assert expected.isdisjoint(inspect(connection).get_table_names())
    finally:
        engine.dispose()
