from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import MetaData, Table, create_engine, inspect
from sqlalchemy.exc import IntegrityError


def test_customer_settlement_migration_upgrade_and_downgrade(tmp_path: Path) -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/c3d4e5f6a7b9_add_customer_settlements.py"
    )
    spec = importlib.util.spec_from_file_location("customer_settlement_migration", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)

    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    expected_tables = {
        "customer_settlement_revision",
        "customer_settlement_balance",
        "customer_settlement_mapping_revision",
        "customer_settlement_mapping_entry",
        "customer_settlement_pilot_access",
        "customer_settlement_assertion_jti",
    }
    try:
        with engine.begin() as connection:
            module.op = Operations(MigrationContext.configure(connection))
            module.upgrade()
            assert expected_tables.issubset(inspect(connection).get_table_names())

        metadata = MetaData()
        revision = Table(
            "customer_settlement_revision",
            metadata,
            autoload_with=engine,
        )
        now = datetime(2026, 7, 29, tzinfo=UTC)

        def values(status: str, source_hash: str) -> dict[str, object]:
            return {
                "status": status,
                "organization_ref": "0x" + "a" * 32,
                "currency": "RUB",
                "as_of": now,
                "source_db_time": now,
                "synced_at": now,
                "source_mode": "synthetic-test",
                "source_hash": source_hash,
                "expected_row_count": 0,
                "loaded_row_count": 0,
                "zero_row_count": 0,
            }

        with engine.begin() as connection:
            connection.execute(
                revision.insert(),
                [values("failed", "a" * 64), values("failed", "b" * 64)],
            )
            connection.execute(revision.insert().values(**values("active", "c" * 64)))
            with pytest.raises(IntegrityError):
                connection.execute(revision.insert().values(**values("active", "d" * 64)))

        with engine.begin() as connection:
            module.op = Operations(MigrationContext.configure(connection))
            module.downgrade()
            assert expected_tables.isdisjoint(inspect(connection).get_table_names())
    finally:
        engine.dispose()


def test_customer_account_guid_migration_upgrade_and_downgrade(tmp_path: Path) -> None:
    versions = Path(__file__).resolve().parents[1] / "alembic/versions"

    def load(filename: str, module_name: str):
        spec = importlib.util.spec_from_file_location(module_name, versions / filename)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        return module

    base = load("c3d4e5f6a7b9_add_customer_settlements.py", "settlement_base_migration")
    account = load(
        "d9e1f3a5b7c9_add_customer_account_guid_mapping.py",
        "settlement_account_migration",
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'account-migration.db'}")
    try:
        with engine.begin() as connection:
            base.op = Operations(MigrationContext.configure(connection))
            base.upgrade()
            account.op = Operations(MigrationContext.configure(connection))
            account.upgrade()
            tables = set(inspect(connection).get_table_names())
            assert {
                "customer_account",
                "customer_account_site_binding",
                "customer_account_source_binding",
            }.issubset(tables)
            balance_columns = {
                item["name"]
                for item in inspect(connection).get_columns("customer_settlement_balance")
            }
            assert "counterparty_guid" in balance_columns

        with engine.begin() as connection:
            account.op = Operations(MigrationContext.configure(connection))
            account.downgrade()
            tables = set(inspect(connection).get_table_names())
            assert "customer_account" not in tables
            balance_columns = {
                item["name"]
                for item in inspect(connection).get_columns("customer_settlement_balance")
            }
            assert "counterparty_guid" not in balance_columns
    finally:
        engine.dispose()


def test_customer_settlement_merge_revision_joins_active_production_head() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/2a4c6e8f0b1d_merge_customer_settlements_with_active_head.py"
    )
    spec = importlib.util.spec_from_file_location("customer_settlement_merge_migration", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)

    assert module.revision == "2a4c6e8f0b1d"
    assert set(module.down_revision) == {"1b9d3f5a7c21", "d9e1f3a5b7c9"}


def test_customer_settlement_merge_revision_joins_current_production_head() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/c9e1f3a5b7d0_merge_settlements_with_current_production_head.py"
    )
    spec = importlib.util.spec_from_file_location(
        "customer_settlement_current_production_merge_migration",
        path,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)

    assert module.revision == "c9e1f3a5b7d0"
    assert set(module.down_revision) == {"b8d0f2a4c6e8", "6e8f0a2b4c6d"}


def test_customer_settlement_operational_migration_upgrade_and_downgrade(
    tmp_path: Path,
) -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/4c6e8a0b2d3f_add_settlement_reconciliation_alerts.py"
    )
    spec = importlib.util.spec_from_file_location("settlement_operational_migration", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    engine = create_engine(f"sqlite:///{tmp_path / 'operational-migration.db'}")
    expected = {
        "customer_settlement_reconciliation_run",
        "customer_settlement_alert_state",
        "customer_settlement_alert_outbox",
    }
    try:
        with engine.begin() as connection:
            module.op = Operations(MigrationContext.configure(connection))
            module.upgrade()
            assert expected.issubset(inspect(connection).get_table_names())
        with engine.begin() as connection:
            module.op = Operations(MigrationContext.configure(connection))
            module.downgrade()
            assert expected.isdisjoint(inspect(connection).get_table_names())
    finally:
        engine.dispose()


def test_customer_settlement_reconciliation_context_migration_upgrade_and_downgrade(
    tmp_path: Path,
) -> None:
    versions = Path(__file__).resolve().parents[1] / "alembic/versions"

    def load(filename: str, module_name: str):
        spec = importlib.util.spec_from_file_location(module_name, versions / filename)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        return module

    operational = load(
        "4c6e8a0b2d3f_add_settlement_reconciliation_alerts.py",
        "settlement_operational_for_context",
    )
    context = load(
        "6e8f0a2b4c6d_bind_settlement_reconciliation_context.py",
        "settlement_reconciliation_context",
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'reconciliation-context.db'}")
    try:
        with engine.begin() as connection:
            operational.op = Operations(MigrationContext.configure(connection))
            operational.upgrade()
            context.op = Operations(MigrationContext.configure(connection))
            context.upgrade()
            columns = {
                item["name"]
                for item in inspect(connection).get_columns(
                    "customer_settlement_reconciliation_run"
                )
            }
            assert {"context_hash", "source_hash", "input_hash"}.issubset(columns)

        reconciliation = Table(
            "customer_settlement_reconciliation_run",
            MetaData(),
            autoload_with=engine,
        )
        values = {
            "report_date": datetime(2026, 8, 22, tzinfo=UTC).date(),
            "as_of": datetime(2026, 8, 22, 21, 0, tzinfo=UTC),
            "report_hash": "a" * 64,
            "context_hash": "b" * 64,
            "source_hash": "c" * 64,
            "status": "matched",
            "expected_count": 10,
            "matched_count": 10,
            "mismatch_count": 0,
            "max_abs_difference": 0,
        }
        with engine.begin() as connection:
            connection.execute(reconciliation.insert().values(**values, input_hash="d" * 64))
            connection.execute(reconciliation.insert().values(**values, input_hash="e" * 64))
            with pytest.raises(IntegrityError):
                connection.execute(reconciliation.insert().values(**values, input_hash="d" * 64))

        with engine.begin() as connection:
            context.op = Operations(MigrationContext.configure(connection))
            context.downgrade()
            columns = {
                item["name"]
                for item in inspect(connection).get_columns(
                    "customer_settlement_reconciliation_run"
                )
            }
            assert {"context_hash", "source_hash", "input_hash"}.isdisjoint(columns)
    finally:
        engine.dispose()
