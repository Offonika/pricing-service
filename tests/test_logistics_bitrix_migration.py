from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, inspect


def _load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/9d1f3a5c7e68_add_logistics_bitrix_stage_outbox.py"
    )
    spec = importlib.util.spec_from_file_location("logistics_bitrix_migration", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def test_logistics_bitrix_migration_upgrade_and_downgrade(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'logistics-bitrix-migration.db'}")
    metadata = MetaData()
    Table(
        "logistics_user",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("external_id", String(64)),
    )
    Table("site_order_execution_case", metadata, Column("id", Integer, primary_key=True))
    Table("site_order_execution_event", metadata, Column("id", Integer, primary_key=True))
    metadata.create_all(engine)
    migration = _load_migration()

    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()

        inspector = inspect(connection)
        assert {
            "logistics_web_launch_token",
            "site_order_stage_outbox",
        } <= set(inspector.get_table_names())
        assert "bitrix_user_id" in {
            column["name"] for column in inspector.get_columns("logistics_user")
        }
        assert {index["name"] for index in inspector.get_indexes("site_order_stage_outbox")} == {
            "ix_site_order_stage_outbox_case_id",
            "ix_site_order_stage_outbox_status_next",
        }

        migration.downgrade()
        inspector = inspect(connection)
        assert "logistics_web_launch_token" not in inspector.get_table_names()
        assert "site_order_stage_outbox" not in inspector.get_table_names()
        assert "bitrix_user_id" not in {
            column["name"] for column in inspector.get_columns("logistics_user")
        }

    engine.dispose()


def test_logistics_bitrix_migration_extends_current_head() -> None:
    assert _load_migration().down_revision == "8c0e2a4b6d57"
