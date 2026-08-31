from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect


def _load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/f5b7c9d1e3a5_add_customer_return_carrier_control.py"
    )
    spec = importlib.util.spec_from_file_location("customer_returns_migration", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def test_customer_returns_migration_upgrade_and_downgrade(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'customer-returns-migration.db'}")
    migration = _load_migration()

    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()

        inspector = inspect(connection)
        assert {
            "customer_return_shipment",
            "customer_return_event",
            "customer_return_action",
        } <= set(inspector.get_table_names())
        assert {index["name"] for index in inspector.get_indexes("customer_return_action")} == {
            "ix_customer_return_action_due",
            "ix_customer_return_action_shipment",
        }
        assert {
            "next_attempt_at",
            "lease_token",
            "leased_until",
        } <= {column["name"] for column in inspector.get_columns("customer_return_action")}
        assert {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("customer_return_shipment")
        } == {
            "uq_customer_return_shipment_carrier_tracking",
            "uq_customer_return_shipment_source_ref",
        }

        migration.downgrade()
        inspector = inspect(connection)
        assert {
            "customer_return_shipment",
            "customer_return_event",
            "customer_return_action",
        }.isdisjoint(inspector.get_table_names())

    engine.dispose()


def test_customer_returns_migration_extends_current_head() -> None:
    assert _load_migration().down_revision == "e4a6c8d0f2b4"
