from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, DateTime, Integer, MetaData, Table, create_engine, inspect


def _load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/d2f4a6b8c0e2_harden_multi_shipment_snapshots.py"
    )
    spec = importlib.util.spec_from_file_location("multi_shipment_hardening_migration", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_multi_shipment_hardening_migration_cycle(tmp_path: Path) -> None:
    migration = _load_migration()
    engine = create_engine(f"sqlite:///{tmp_path / 'multi-shipment-migration.db'}")
    metadata = MetaData()
    Table(
        "site_order_rtu",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("updated_at", DateTime, nullable=False),
    )
    Table(
        "site_order_shipment",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("case_id", Integer, nullable=False),
        Column("updated_at", DateTime, nullable=False),
    )
    Table(
        "site_order_shipment_notification",
        metadata,
        Column("id", Integer, primary_key=True),
    )
    metadata.create_all(engine)

    try:
        with engine.begin() as connection:
            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()
            inspector = inspect(connection)
            assert {"active", "last_seen_snapshot_id", "retired_at", "source_revision"} <= {
                item["name"] for item in inspector.get_columns("site_order_rtu")
            }
            assert {
                "active",
                "last_seen_snapshot_id",
                "retired_at",
                "source_revision",
                "part_number",
                "legacy_owned",
            } <= {item["name"] for item in inspector.get_columns("site_order_shipment")}
            assert {"submitted_at", "delivered_at", "failed_at"} <= {
                item["name"] for item in inspector.get_columns("site_order_shipment_notification")
            }
            part_index = next(
                item
                for item in inspector.get_indexes("site_order_shipment")
                if item["name"] == "uq_site_order_shipment_case_part"
            )
            assert part_index["unique"] == 1
            assert part_index["column_names"] == ["case_id", "part_number"]

            migration.downgrade()
            inspector = inspect(connection)
            assert "active" not in {
                item["name"] for item in inspector.get_columns("site_order_rtu")
            }
            assert "part_number" not in {
                item["name"] for item in inspector.get_columns("site_order_shipment")
            }
            assert "submitted_at" not in {
                item["name"] for item in inspector.get_columns("site_order_shipment_notification")
            }

            migration.upgrade()
            inspector = inspect(connection)
            assert "part_number" in {
                item["name"] for item in inspector.get_columns("site_order_shipment")
            }
    finally:
        engine.dispose()
