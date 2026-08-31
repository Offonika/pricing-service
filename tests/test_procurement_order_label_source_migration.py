from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


def _load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/a5d7e9f1b3c4_add_procurement_label_source.py"
    )
    spec = importlib.util.spec_from_file_location("procurement_label_source_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_procurement_label_source_migration_upgrade_and_downgrade(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'procurement-label-source.db'}")
    migration = _load_migration()
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE procurement_order_formation (id INTEGER PRIMARY KEY)")
        )
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()

        columns = {
            column["name"]
            for column in inspect(connection).get_columns("procurement_order_formation")
        }
        assert {
            "label_onec_document_number",
            "label_onec_document_date",
            "label_source_linked_at",
        } <= columns

        migration.downgrade()
        columns = {
            column["name"]
            for column in inspect(connection).get_columns("procurement_order_formation")
        }
        assert columns == {"id"}
    engine.dispose()


def test_procurement_label_source_migration_extends_active_head() -> None:
    assert _load_migration().down_revision == "f5b7c9d1e3a5"
