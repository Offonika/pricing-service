from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect


def _load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/b9e5d7f3a012_add_customer_price_type_core.py"
    )
    spec = importlib.util.spec_from_file_location("customer_price_type_migration", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _load_quality_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/c0f1e2d3a456_add_customer_price_type_quality_samples.py"
    )
    spec = importlib.util.spec_from_file_location("customer_price_type_quality_migration", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _load_review_batch_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/e2f3a4b5c6d8_add_customer_price_type_review_batches.py"
    )
    spec = importlib.util.spec_from_file_location(
        "customer_price_type_review_batch_migration", path
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _load_reviews_v2_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/d4f6a8c0e2b3_customer_price_type_reviews_v2.py"
    )
    spec = importlib.util.spec_from_file_location("customer_price_type_reviews_v2_migration", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_migration_upgrade_and_downgrade_with_circular_foreign_keys(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    migration = _load_migration()
    try:
        with engine.begin() as connection:
            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()

            inspector = inspect(connection)
            expected = {
                "customer_price_type_profile",
                "customer_price_type_run",
                "customer_price_type_snapshot",
                "customer_price_type_case",
                "customer_price_type_case_event",
            }
            assert expected <= set(inspector.get_table_names())
            profile_fks = {
                item["name"] for item in inspector.get_foreign_keys("customer_price_type_profile")
            }
            assert profile_fks == {
                "fk_customer_price_type_profile_latest_snapshot",
                "fk_customer_price_type_profile_open_case",
            }

            migration.downgrade()
            assert expected.isdisjoint(inspect(connection).get_table_names())
    finally:
        engine.dispose()


def test_quality_sample_migration_upgrade_and_downgrade(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'quality-migration.db'}")
    core = _load_migration()
    quality = _load_quality_migration()
    try:
        with engine.begin() as connection:
            operations = Operations(MigrationContext.configure(connection))
            core.op = operations
            core.upgrade()
            quality.op = operations
            quality.upgrade()

            inspector = inspect(connection)
            assert "customer_price_type_quality_sample" in inspector.get_table_names()
            assert {
                "run_id",
                "snapshot_id",
                "profile_id",
                "system_group",
                "correct_group",
                "status",
                "version",
            } <= {
                item["name"] for item in inspector.get_columns("customer_price_type_quality_sample")
            }

            quality.downgrade()
            assert "customer_price_type_quality_sample" not in inspect(connection).get_table_names()
            core.downgrade()
    finally:
        engine.dispose()


def test_review_batch_migration_upgrade_and_downgrade(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'review-batch-migration.db'}")
    migration = _load_review_batch_migration()
    try:
        with engine.begin() as connection:
            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()
            inspector = inspect(connection)
            assert {
                "customer_price_type_review_batch",
                "customer_price_type_review_batch_item",
            } <= set(inspector.get_table_names())
            assert {
                "counterparty_ref",
                "counterparty_code",
                "expected_bucket",
                "expected_price_type",
            } <= {
                item["name"]
                for item in inspector.get_columns("customer_price_type_review_batch_item")
            }
            migration.downgrade()
            assert "customer_price_type_review_batch" not in inspect(connection).get_table_names()
    finally:
        engine.dispose()


def test_reviews_v2_migration_upgrade_and_downgrade(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'reviews-v2-migration.db'}")
    core = _load_migration()
    reviews_v2 = _load_reviews_v2_migration()
    try:
        with engine.begin() as connection:
            core.op = Operations(MigrationContext.configure(connection))
            core.upgrade()
            reviews_v2.op = Operations(MigrationContext.configure(connection))
            reviews_v2.upgrade()

            inspector = inspect(connection)
            assert {
                "customer_price_type_review",
                "customer_price_type_external_action",
                "customer_price_type_onec_contract_action",
            } <= set(inspector.get_table_names())
            assert {"review_kind", "snapshot_hash", "decision_mode"} <= {
                item["name"] for item in inspector.get_columns("customer_price_type_review")
            }
            assert {"execution_allowed_at_decision", "status", "payload"} <= {
                item["name"]
                for item in inspector.get_columns("customer_price_type_external_action")
            }

            reviews_v2.downgrade()
            assert "customer_price_type_review" not in inspect(connection).get_table_names()
            core.downgrade()
    finally:
        engine.dispose()
