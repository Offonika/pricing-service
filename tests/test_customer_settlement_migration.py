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
