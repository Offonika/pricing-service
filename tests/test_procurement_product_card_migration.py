from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect

PROJECT = Path(__file__).resolve().parents[1]
MIGRATION = PROJECT / "alembic/versions/c6d7e8f90123_add_procurement_product_card_sync_state.py"


def test_procurement_product_card_sync_state_migration_upgrades_and_downgrades(
    tmp_path: Path,
) -> None:
    spec = importlib.util.spec_from_file_location(
        "procurement_product_card_sync_state_migration",
        MIGRATION,
    )
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine(f"sqlite:///{tmp_path / 'product-card-migration.db'}")
    try:
        with engine.begin() as connection:
            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()

            inspector = inspect(connection)
            assert "procurement_product_card_sync_state" in inspector.get_table_names()
            columns = {
                column["name"]
                for column in inspector.get_columns("procurement_product_card_sync_state")
            }
            assert {
                "product_xml_id",
                "bitrix_product_id",
                "snapshot_hash",
                "desired_fields",
                "readback_fields",
                "status",
                "last_attempt_at",
                "last_success_at",
                "last_error",
            }.issubset(columns)
            indexes = {
                item["name"]
                for item in inspector.get_indexes("procurement_product_card_sync_state")
            }
            assert {
                "ix_proc_product_card_sync_bitrix_product",
                "ix_proc_product_card_sync_status",
            }.issubset(indexes)

            migration.downgrade()
            assert (
                "procurement_product_card_sync_state" not in inspect(connection).get_table_names()
            )
    finally:
        engine.dispose()
