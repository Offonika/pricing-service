from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect

PROJECT = Path(__file__).resolve().parents[1]
MIGRATION = PROJECT / "alembic/versions/d7e8f9012345_add_order_assembly_crm_outbox.py"


def test_order_assembly_outbox_migration_upgrades_and_downgrades() -> None:
    spec = importlib.util.spec_from_file_location("order_assembly_outbox_migration", MIGRATION)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite://")

    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        inspector = inspect(connection)
        assert "order_assembly_crm_outbox" in set(inspector.get_table_names())
        indexes = {row["name"] for row in inspector.get_indexes("order_assembly_crm_outbox")}
        assert "ix_order_assembly_crm_outbox_status_next" in indexes
        assert "ix_order_assembly_crm_outbox_order" in indexes

        migration.downgrade()
        assert "order_assembly_crm_outbox" not in set(inspect(connection).get_table_names())
