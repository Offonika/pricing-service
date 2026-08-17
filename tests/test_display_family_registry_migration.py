from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


def _load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/0a8c2e4f6b7d_add_display_family_registry.py"
    )
    spec = importlib.util.spec_from_file_location("display_family_registry_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_display_family_registry_migration_upgrade_and_downgrade(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'display-family.db'}")
    migration = _load_migration()
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE product (id INTEGER PRIMARY KEY AUTOINCREMENT)"))
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()

        inspector = inspect(connection)
        assert inspector.has_table("display_family_registry_version")
        assert inspector.has_table("display_family")
        assert inspector.has_table("display_family_member")
        assert inspector.has_table("display_family_decision_event")
        version_columns = {
            column["name"] for column in inspector.get_columns("display_family_registry_version")
        }
        assert {
            "inventory_checksum",
            "membership_checksum",
            "effective_from",
            "source_manifest_json",
        } <= version_columns
        indexes = inspector.get_indexes("display_family_registry_version")
        assert any(
            index["name"] == "uq_display_family_registry_single_active" and index["unique"]
            for index in indexes
        )

        migration.downgrade()
        inspector = inspect(connection)
        assert not inspector.has_table("display_family_decision_event")
        assert not inspector.has_table("display_family_member")
        assert not inspector.has_table("display_family")
        assert not inspector.has_table("display_family_registry_version")
        assert inspector.has_table("product")
    engine.dispose()


def test_display_family_registry_migration_is_isolated_from_pending_schema_branch() -> None:
    migration = _load_migration()
    versions = Path(__file__).resolve().parents[1] / "alembic/versions"

    assert migration.down_revision == "c3e5a7b9d1f2"
    assert not (versions / "d4f6a8c0e2b3_customer_price_type_reviews_v2.py").exists()
    assert not (versions / "e5a7c9d1f3b4_add_assortment_lifecycle_v2_shadow.py").exists()
    assert not (versions / "f6b8d0e2a4c5_add_assortment_stock_inflow_dates.py").exists()
    assert not (versions / "1b9d3f5a7c8e_merge_display_family_registry.py").exists()
