from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect


def _migration_module():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/a7b8c9d0e2f3_add_nomenclature_classification_transport.py"
    )
    spec = importlib.util.spec_from_file_location("nomenclature_classification_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_nomenclature_classification_migration_upgrade_and_downgrade() -> None:
    engine = create_engine("sqlite:///:memory:")
    migration = _migration_module()
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        inspector = inspect(connection)
        tables = set(inspector.get_table_names())
        assert {
            "nomenclature_classification_operation",
            "nomenclature_classification_operation_item",
            "nomenclature_classification_operation_event",
        } <= tables
        operation_columns = {
            column["name"]
            for column in inspector.get_columns("nomenclature_classification_operation")
        }
        assert {
            "operation_id",
            "command_hash",
            "state",
            "canonical_payload",
            "dry_run_message_id",
            "apply_message_id",
            "readback_message_id",
        } <= operation_columns

        migration.downgrade()
        assert not {
            "nomenclature_classification_operation",
            "nomenclature_classification_operation_item",
            "nomenclature_classification_operation_event",
        }.intersection(inspect(connection).get_table_names())
