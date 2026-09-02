from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError


def _load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/d8e0f2a4b6c8_unique_procurement_onec_ref.py"
    )
    spec = importlib.util.spec_from_file_location("procurement_onec_ref_unique_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _prepare(connection) -> None:
    connection.execute(text("""
        CREATE TABLE procurement_order_formation (
          id INTEGER PRIMARY KEY,
          onec_document_ref VARCHAR(64)
        )
    """))


def test_unique_onec_ref_migration_audits_then_constrains_normalized_guid(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'unique-onec-ref.db'}")
    migration = _load_migration()
    with engine.begin() as connection:
        _prepare(connection)
        connection.execute(
            text(
                "INSERT INTO procurement_order_formation (id, onec_document_ref) VALUES "
                "(1, '0xabcdef'), (2, NULL), (3, '')"
            )
        )
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        index_sql = connection.scalar(
            text("SELECT sql FROM sqlite_master WHERE type = 'index' AND name = :name"),
            {"name": migration.INDEX_NAME},
        )
        assert "lower(trim(onec_document_ref))" in str(index_sql)
        with pytest.raises(IntegrityError, match="UNIQUE constraint failed"):
            connection.execute(
                text(
                    "INSERT INTO procurement_order_formation (id, onec_document_ref) "
                    "VALUES (4, ' 0xABCDEF ')"
                )
            )
    engine.dispose()


def test_unique_onec_ref_migration_blocks_existing_duplicates(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'duplicate-onec-ref.db'}")
    migration = _load_migration()
    with engine.begin() as connection:
        _prepare(connection)
        connection.execute(
            text(
                "INSERT INTO procurement_order_formation (id, onec_document_ref) VALUES "
                "(1, '0xabcdef'), (2, ' 0xABCDEF ')"
            )
        )
        migration.op = Operations(MigrationContext.configure(connection))
        with pytest.raises(RuntimeError, match="duplicate procurement orders"):
            migration.upgrade()
    engine.dispose()
