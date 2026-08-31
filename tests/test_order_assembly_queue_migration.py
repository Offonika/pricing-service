from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect

PROJECT = Path(__file__).resolve().parents[1]
MIGRATION = PROJECT / "alembic/versions/a4c5e6f70819_add_order_assembly_queue.py"


def test_order_assembly_queue_migration_upgrades_and_downgrades() -> None:
    spec = importlib.util.spec_from_file_location("order_assembly_queue_migration", MIGRATION)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite://")

    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        tables = set(inspect(connection).get_table_names())
        assert "order_assembly_queue_item" in tables
        assert "order_assembly_queue_sync_state" in tables
        indexes = {
            row["name"] for row in inspect(connection).get_indexes("order_assembly_queue_item")
        }
        assert "ix_order_assembly_queue_item_order_number" in indexes
        assert "ix_order_assembly_queue_item_priority" in indexes

        migration.downgrade()
        tables = set(inspect(connection).get_table_names())
        assert "order_assembly_queue_item" not in tables
        assert "order_assembly_queue_sync_state" not in tables
