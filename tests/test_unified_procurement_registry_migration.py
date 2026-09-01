from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


def _load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/b6e8f0a2c4d6_add_unified_procurement_registry.py"
    )
    spec = importlib.util.spec_from_file_location("unified_procurement_registry_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_unified_registry_migration_upgrade_and_downgrade(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'unified-orders.db'}")
    migration = _load_migration()
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE procurement_order_formation (
              id INTEGER PRIMARY KEY, status VARCHAR(32) NOT NULL,
              order_date DATE NOT NULL, onec_document_number VARCHAR(64), onec_document_ref VARCHAR(64)
            )
        """))
        connection.execute(
            text("CREATE TABLE procurement_order_formation_line (id INTEGER PRIMARY KEY)")
        )
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        order_columns = {
            item["name"] for item in inspect(connection).get_columns("procurement_order_formation")
        }
        line_columns = {
            item["name"]
            for item in inspect(connection).get_columns("procurement_order_formation_line")
        }
        assert {
            "lifecycle_status",
            "origin",
            "onec_open_quantity",
            "last_onec_sync_at",
        } <= order_columns
        assert {"onec_open_quantity", "onec_received_quantity"} <= line_columns

        migration.downgrade()
        order_columns = {
            item["name"] for item in inspect(connection).get_columns("procurement_order_formation")
        }
        assert "lifecycle_status" not in order_columns
    engine.dispose()


def test_unified_registry_migration_extends_active_head() -> None:
    assert _load_migration().down_revision == "a5d7e9f1b3c4"
