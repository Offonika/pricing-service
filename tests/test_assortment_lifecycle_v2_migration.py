from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


def _load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/e5a7c9d1f3b4_add_assortment_lifecycle_v2_shadow.py"
    )
    spec = importlib.util.spec_from_file_location("assortment_lifecycle_v2_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_assortment_lifecycle_v2_migration_upgrade_and_downgrade(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'lifecycle-v2.db'}")
    migration = _load_migration()
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE assortment_lifecycle_classification_run ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE assortment_lifecycle_classification ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "nomenclature_code VARCHAR(64) NOT NULL UNIQUE)"
            )
        )
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        columns = {
            column["name"]
            for column in inspect(connection).get_columns("assortment_lifecycle_classification")
        }
        assert {"demand_state", "first_receipt_at", "cost_quartile"} <= columns
        assert inspect(connection).has_table("assortment_lifecycle_classification_history")
        migration.downgrade()
        columns = {
            column["name"]
            for column in inspect(connection).get_columns("assortment_lifecycle_classification")
        }
        assert "demand_state" not in columns
        assert not inspect(connection).has_table("assortment_lifecycle_classification_history")
    engine.dispose()
