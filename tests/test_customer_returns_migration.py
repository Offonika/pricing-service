from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, inspect, select


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


def test_customer_return_deal_link_migration(tmp_path: Path) -> None:
    base_migration = _load_migration()
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/d8e0f2a4c6b9_add_customer_return_deal_link.py"
    )
    spec = importlib.util.spec_from_file_location("customer_return_deal_link_migration", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.down_revision == "c7d9e1f3a5b8"

    engine = create_engine(f"sqlite:///{tmp_path / 'customer-return-deal-link.db'}")
    with engine.begin() as connection:
        base_migration.op = Operations(MigrationContext.configure(connection))
        base_migration.upgrade()
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()

        inspector = inspect(connection)
        columns = {column["name"] for column in inspector.get_columns("customer_return_shipment")}
        assert {
            "bitrix_deal_id",
            "bitrix_deal_title",
            "bitrix_order_ref",
            "bitrix_contact_id",
            "bitrix_company_id",
            "bitrix_responsible_user_id",
            "bitrix_deal_linked_at",
        } <= columns
        assert "ix_customer_return_shipment_bitrix_deal" in {
            index["name"] for index in inspector.get_indexes("customer_return_shipment")
        }

        migration.downgrade()
        remaining = {
            column["name"] for column in inspect(connection).get_columns("customer_return_shipment")
        }
        assert "bitrix_deal_id" not in remaining

    engine.dispose()


def test_customer_return_service_link_migration_backfills_legacy_ids(tmp_path: Path) -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/b7c8d9e0f1a2_add_customer_return_service_links.py"
    )
    spec = importlib.util.spec_from_file_location("customer_return_service_link_migration", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.down_revision == "c6d7e8f90123"

    engine = create_engine(f"sqlite:///{tmp_path / 'customer-return-service-link.db'}")
    metadata = MetaData()
    shipments = Table(
        "customer_return_shipment",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("bitrix_case_id", String(64)),
        Column("site_ticket_id", String(64)),
    )
    service_cases = Table(
        "site_service_request_case",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("source_ticket_id", Integer, nullable=False),
        Column("bitrix_item_id", Integer),
    )
    Table("expertise_case", metadata, Column("id", Integer, primary_key=True))
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(
            service_cases.insert(),
            [{"source_ticket_id": 7002, "bitrix_item_id": 113402}],
        )
        connection.execute(
            shipments.insert(),
            [
                {"id": 1, "bitrix_case_id": "113401", "site_ticket_id": None},
                {"id": 2, "bitrix_case_id": "CASE-OLD", "site_ticket_id": "7002"},
                {"id": 3, "bitrix_case_id": "CASE-UNKNOWN", "site_ticket_id": "7999"},
            ],
        )
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()

        upgraded = Table("customer_return_shipment", MetaData(), autoload_with=connection)
        assert connection.execute(
            select(upgraded.c.id, upgraded.c.service_request_item_id).order_by(upgraded.c.id)
        ).all() == [(1, 113401), (2, 113402), (3, None)]
        assert {
            "service_request_item_id",
            "service_request_deal_id",
            "service_request_linked_at",
        } <= {column["name"] for column in inspect(connection).get_columns(shipments.name)}

        # An application rollback is safe without a data rollback: all pre-release
        # columns and values remain readable while the new columns keep their data.
        legacy_rows = connection.execute(
            select(upgraded.c.id, upgraded.c.bitrix_case_id, upgraded.c.site_ticket_id).order_by(
                upgraded.c.id
            )
        ).all()
        assert legacy_rows == [
            (1, "113401", None),
            (2, "CASE-OLD", "7002"),
            (3, "CASE-UNKNOWN", "7999"),
        ]

    engine.dispose()
