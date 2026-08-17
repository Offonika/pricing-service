from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


def _load_migration(filename: str, module_name: str) -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "alembic/versions" / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_assortment_lifecycle_signal_migration_upgrade_and_downgrade(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'assortment-signal.db'}")
    migration = _load_migration(
        "2c4e6a8b0d1f_add_assortment_lifecycle_signal.py",
        "assortment_lifecycle_signal_migration",
    )

    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()

        inspector = inspect(connection)
        assert inspector.has_table("assortment_lifecycle_signal")
        columns = {
            column["name"] for column in inspector.get_columns("assortment_lifecycle_signal")
        }
        assert {
            "occurred_at",
            "available_at",
            "nomenclature_code",
            "display_family_key",
            "display_family_registry_version",
            "payload_hash",
        } <= columns
        constraint_names = {
            constraint["name"]
            for constraint in inspector.get_check_constraints("assortment_lifecycle_signal")
        }
        assert {
            "ck_assortment_lifecycle_signal_available_after_occurrence",
            "ck_assortment_lifecycle_signal_family_version",
            "ck_assortment_lifecycle_signal_wordstat_direction_only",
        } <= constraint_names
        index_names = {
            index["name"] for index in inspector.get_indexes("assortment_lifecycle_signal")
        }
        assert {
            "ix_assortment_lifecycle_signal_available_type",
            "ix_assortment_lifecycle_signal_source_event",
        } <= index_names

        connection.execute(
            text("""
                INSERT INTO assortment_lifecycle_signal (
                    schema_version,
                    signal_key,
                    signal_type,
                    source,
                    source_event_id,
                    occurred_at,
                    available_at,
                    nomenclature_code,
                    reliability,
                    reliability_reason,
                    quantity,
                    payload,
                    payload_hash
                ) VALUES (
                    'assortment_signal.v1',
                    :signal_key,
                    'site_order',
                    'site',
                    'order-1001',
                    '2026-08-17 09:00:00',
                    '2026-08-17 09:01:00',
                    'SKU-17PM-001',
                    0.95,
                    'validated_site_event',
                    1,
                    '{}',
                    :payload_hash
                )
                """),
            {"signal_key": "a" * 64, "payload_hash": "b" * 64},
        )
        with pytest.raises(
            IntegrityError,
            match="assortment_lifecycle_signal_is_append_only",
        ):
            connection.execute(
                text(
                    "UPDATE assortment_lifecycle_signal "
                    "SET reliability_reason = 'changed' WHERE signal_key = :signal_key"
                ),
                {"signal_key": "a" * 64},
            )
        with pytest.raises(
            IntegrityError,
            match="assortment_lifecycle_signal_is_append_only",
        ):
            connection.execute(
                text("DELETE FROM assortment_lifecycle_signal " "WHERE signal_key = :signal_key"),
                {"signal_key": "a" * 64},
            )

        migration.downgrade()
        assert not inspect(connection).has_table("assortment_lifecycle_signal")
    engine.dispose()


def test_signal_store_migration_is_isolated_then_merged() -> None:
    migration = _load_migration(
        "2c4e6a8b0d1f_add_assortment_lifecycle_signal.py",
        "assortment_lifecycle_signal_migration_graph",
    )
    merge = _load_migration(
        "3d5f7b9c1e2a_merge_assortment_lifecycle_signal.py",
        "assortment_lifecycle_signal_merge",
    )

    assert migration.down_revision == "0a8c2e4f6b7d"
    assert set(merge.down_revision) == {"1b9d3f5a7c8e", "2c4e6a8b0d1f"}
