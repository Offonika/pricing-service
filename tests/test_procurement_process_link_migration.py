from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


def _load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/c7d9e1f3a5b8_add_procurement_process_link_state.py"
    )
    spec = importlib.util.spec_from_file_location("procurement_process_link_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_process_link_migration_upgrade_and_downgrade(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'process-link.db'}")
    migration = _load_migration()
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE procurement_order_formation (id INTEGER PRIMARY KEY)")
        )
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        columns = {
            item["name"] for item in inspect(connection).get_columns("procurement_order_formation")
        }
        assert {"bitrix_stage_name", "bitrix_link_checked_at", "bitrix_link_error"} <= columns

        migration.downgrade()
        columns = {
            item["name"] for item in inspect(connection).get_columns("procurement_order_formation")
        }
        assert "bitrix_link_checked_at" not in columns
    engine.dispose()


def test_process_link_migration_extends_unified_registry_head() -> None:
    assert _load_migration().down_revision == "b6e8f0a2c4d6"
