from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, inspect

PROJECT = Path(__file__).resolve().parents[1]
MIGRATION = PROJECT / "alembic/versions/b5d6e7f80920_add_logistics_order_transfer_plans.py"


def _load_migration():
    spec = importlib.util.spec_from_file_location("logistics_order_plan_migration", MIGRATION)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def test_logistics_order_plan_migration_upgrades_and_downgrades(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'order-plan-migration.db'}")
    metadata = MetaData()
    Table("logistics_warehouse", metadata, Column("id", Integer, primary_key=True))
    Table(
        "logistics_transfer",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("origin_order_external_id", String(64)),
    )
    metadata.create_all(engine)
    migration = _load_migration()

    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        inspector = inspect(connection)
        assert {
            "logistics_order_plan",
            "logistics_order_plan_unit",
        } <= set(inspector.get_table_names())
        assert {
            "flow_mode",
            "plan_key",
            "plan_version",
            "unit_key",
            "expected_unit_count",
            "ready_for_handoff",
            "is_required",
        } <= {column["name"] for column in inspector.get_columns("logistics_transfer")}
        assert "ux_logistics_order_plan_active_order" in {
            index["name"] for index in inspector.get_indexes("logistics_order_plan")
        }

        migration.downgrade()
        inspector = inspect(connection)
        assert "logistics_order_plan" not in inspector.get_table_names()
        assert "logistics_order_plan_unit" not in inspector.get_table_names()
        assert "flow_mode" not in {
            column["name"] for column in inspector.get_columns("logistics_transfer")
        }

    engine.dispose()


def test_logistics_order_plan_migration_extends_assembly_queue_head() -> None:
    assert _load_migration().down_revision == "a4c5e6f70819"
