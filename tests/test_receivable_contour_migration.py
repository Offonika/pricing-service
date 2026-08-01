from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect


def _load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/f4a5b6c7d8e9_add_receivable_bitrix_link.py"
    )
    spec = importlib.util.spec_from_file_location("receivable_contour_migration", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def test_receivable_contour_migration_upgrade_and_downgrade(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    migration = _load_migration()
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        inspector = inspect(connection)

        assert {
            "receivable_bitrix_link",
            "receivable_folder_change_operation",
        }.issubset(inspector.get_table_names())
        link_uniques = {
            item["name"] for item in inspector.get_unique_constraints("receivable_bitrix_link")
        }
        assert link_uniques == {
            "uq_receivable_bitrix_link_work_contour",
            "uq_receivable_bitrix_link_contour_item",
        }
        folder_uniques = {
            item["name"]
            for item in inspector.get_unique_constraints("receivable_folder_change_operation")
        }
        assert "uq_receivable_folder_change_active_counterparty" in folder_uniques
        checks = inspector.get_check_constraints("receivable_folder_change_operation")
        assert {item["name"] for item in checks} == {"ck_receivable_folder_change_state"}
        assert "needs_review" in str(checks[0]["sqltext"])

        migration.downgrade()
        assert "receivable_bitrix_link" not in inspect(connection).get_table_names()
        assert "receivable_folder_change_operation" not in inspect(connection).get_table_names()
