from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


def _load_migration(
    filename: str = "0a8c2e4f6b7d_add_display_family_registry.py",
):
    path = Path(__file__).resolve().parents[1] / "alembic/versions" / filename
    module_name = f"migration_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
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


def test_display_family_registry_migration_is_merged_with_pending_schema_branch() -> None:
    migration = _load_migration()
    customer_reviews = _load_migration("d4f6a8c0e2b3_customer_price_type_reviews_v2.py")
    lifecycle_v2 = _load_migration("e5a7c9d1f3b4_add_assortment_lifecycle_v2_shadow.py")
    stock_inflow = _load_migration("f6b8d0e2a4c5_add_assortment_stock_inflow_dates.py")
    schema_merge = _load_migration("1b9d3f5a7c8e_merge_display_family_registry.py")
    signal_store = _load_migration("2c4e6a8b0d1f_add_assortment_lifecycle_signal.py")
    final_merge = _load_migration("3d5f7b9c1e2a_merge_assortment_lifecycle_signal.py")

    assert migration.down_revision == "c3e5a7b9d1f2"
    assert customer_reviews.down_revision == "c3e5a7b9d1f2"
    assert lifecycle_v2.down_revision == customer_reviews.revision
    assert stock_inflow.down_revision == lifecycle_v2.revision
    assert schema_merge.down_revision == (migration.revision, stock_inflow.revision)
    assert signal_store.down_revision == migration.revision
    assert final_merge.down_revision == (schema_merge.revision, signal_store.revision)
