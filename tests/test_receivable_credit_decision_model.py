from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.receivable_credit_decision import ReceivableCreditDecisionOperation


def _operation(**overrides: object) -> ReceivableCreditDecisionOperation:
    values: dict[str, object] = {
        "bitrix_entity_type_id": 1200,
        "bitrix_item_id": "2494",
        "bitrix_category_id": 44,
        "bitrix_stage_id": "DT1200_44:APPROVED",
        "bitrix_revision": "7",
        "moved_by_user_id": "115204",
        "decision_id": "2494",
        "decision_hash": "a" * 64,
        "counterparty_key": "a7d9b21e-222e-11ed-8fda-0025901e48ee",
        "active_counterparty_key": "a7d9b21e-222e-11ed-8fda-0025901e48ee",
        "counterparty_ref": "0X8FDA0025901E48EE11ED222EA7D9B21E",
        "counterparty_guid": "a7d9b21e-222e-11ed-8fda-0025901e48ee",
        "counterparty_code": "РБ030337",
        "counterparty_name": "Тестовый контрагент",
        "contract_ref": "0X8266002590803DAF11F143B8070BC34D",
        "contract_guid": "070bc34d-43b8-11f1-8266-002590803daf",
        "contract_code": "РБ0058149",
        "contract_name": "Основной договор1",
        "contract_organization_ref": "0X44445555555555553333222211111111",
        "contract_organization_guid": "11111111-2222-3333-4444-555555555555",
        "contract_organization_code": "000000001",
        "contract_organization_name": "MASTER MOBILE",
        "expected_current_limit": Decimal("100000"),
        "expected_current_depth": 7,
        "expected_current_debt_control_enabled": True,
        "proposed_limit": Decimal("150000"),
        "proposed_depth": 14,
        "proposed_debt_control_enabled": True,
        "currency": "RUB",
        "reason": "Утверждено",
        "approved_by": "115204",
        "approved_at": datetime(2026, 7, 28, tzinfo=timezone.utc),
        "state": "pending_dry_run",
        "source_payload": {},
    }
    values.update(overrides)
    return ReceivableCreditDecisionOperation(**values)


def test_active_counterparty_lock_prevents_parallel_operations(sqlite_engine) -> None:
    from app.models import Base

    Base.metadata.create_all(sqlite_engine)
    try:
        with Session(sqlite_engine) as session:
            session.add(_operation())
            session.commit()
            session.add(
                _operation(
                    bitrix_item_id="2495",
                    decision_id="2495",
                    decision_hash="b" * 64,
                )
            )
            with pytest.raises(IntegrityError):
                session.commit()
    finally:
        Base.metadata.drop_all(sqlite_engine)


def test_completed_operation_releases_counterparty_lock(sqlite_engine) -> None:
    from app.models import Base

    Base.metadata.create_all(sqlite_engine)
    try:
        with Session(sqlite_engine) as session:
            first = _operation()
            session.add(first)
            session.commit()
            first.active_counterparty_key = None
            first.state = "applied"
            session.commit()
            session.add(
                _operation(
                    bitrix_item_id="2495",
                    decision_id="2495",
                    decision_hash="b" * 64,
                )
            )
            session.commit()
    finally:
        Base.metadata.drop_all(sqlite_engine)


def test_item_revision_cannot_be_reused_with_another_hash(sqlite_engine) -> None:
    from app.models import Base

    Base.metadata.create_all(sqlite_engine)
    try:
        with Session(sqlite_engine) as session:
            session.add(_operation(active_counterparty_key=None, state="failed"))
            session.commit()
            session.add(
                _operation(
                    decision_hash="b" * 64,
                    active_counterparty_key=None,
                    state="failed",
                )
            )
            with pytest.raises(IntegrityError):
                session.commit()
    finally:
        Base.metadata.drop_all(sqlite_engine)


def test_database_rejects_non_rub_operation(sqlite_engine) -> None:
    from app.models import Base

    Base.metadata.create_all(sqlite_engine)
    try:
        with Session(sqlite_engine) as session:
            session.add(_operation(currency="USD"))
            with pytest.raises(IntegrityError):
                session.commit()
    finally:
        Base.metadata.drop_all(sqlite_engine)


def test_migration_upgrade_and_downgrade(tmp_path: Path) -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/e2b3c4d5e6f8_add_receivable_credit_decision_operation.py"
    )
    spec = importlib.util.spec_from_file_location("credit_decision_migration", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)

    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    try:
        with engine.begin() as connection:
            module.op = Operations(MigrationContext.configure(connection))
            module.upgrade()
            assert "receivable_credit_decision_operation" in inspect(connection).get_table_names()
            columns = {
                item["name"]: item["type"]
                for item in inspect(connection).get_columns("receivable_credit_decision_operation")
            }
            for name in (
                "dry_run_message_id",
                "apply_message_id",
                "readback_message_id",
            ):
                assert columns[name].length == 120
            constraints = {
                item["name"]: item["column_names"]
                for item in inspect(connection).get_unique_constraints(
                    "receivable_credit_decision_operation"
                )
            }
            assert constraints["uq_receivable_credit_decision_item_revision"] == [
                "bitrix_entity_type_id",
                "bitrix_item_id",
                "bitrix_revision",
            ]
            module.downgrade()
            assert (
                "receivable_credit_decision_operation" not in inspect(connection).get_table_names()
            )
    finally:
        engine.dispose()


def test_contract_identity_migration_preserves_legacy_rows(tmp_path: Path) -> None:
    versions = Path(__file__).resolve().parents[1] / "alembic/versions"
    base_path = versions / "e2b3c4d5e6f8_add_receivable_credit_decision_operation.py"
    contract_path = versions / "d5e6f7a8b9c1_add_credit_decision_contract_identity.py"
    base_spec = importlib.util.spec_from_file_location("credit_decision_base", base_path)
    contract_spec = importlib.util.spec_from_file_location(
        "credit_decision_contract", contract_path
    )
    base_module = importlib.util.module_from_spec(base_spec)
    contract_module = importlib.util.module_from_spec(contract_spec)
    assert base_spec and base_spec.loader and contract_spec and contract_spec.loader
    base_spec.loader.exec_module(base_module)
    contract_spec.loader.exec_module(contract_module)

    engine = create_engine(f"sqlite:///{tmp_path / 'contract-migration.db'}")
    try:
        with engine.begin() as connection:
            operations = Operations(MigrationContext.configure(connection))
            base_module.op = operations
            contract_module.op = operations
            base_module.upgrade()
            connection.exec_driver_sql(
                """
                INSERT INTO receivable_credit_decision_operation (
                    bitrix_entity_type_id, bitrix_item_id, bitrix_stage_id,
                    bitrix_revision, moved_by_user_id, decision_id, decision_hash,
                    counterparty_key, counterparty_ref, counterparty_guid,
                    counterparty_code, counterparty_name, expected_current_limit,
                    expected_current_depth, proposed_limit, proposed_depth, currency,
                    reason, approved_by, approved_at, state, dry_run_attempts,
                    apply_attempts, readback_attempts, bitrix_sync_pending,
                    source_payload
                ) VALUES (
                    1200, 'legacy', 'APPROVED', '1', '115204', 'legacy',
                    :decision_hash, 'counterparty', 'ref', 'guid', 'code', 'name',
                    0, 0, 0, 0, 'RUB', 'legacy', '115204', '2026-08-03',
                    'failed', 0, 0, 0, 0, '{}'
                )
                """,
                {"decision_hash": "a" * 64},
            )
            contract_module.upgrade()
            columns = {
                item["name"]: item
                for item in inspect(connection).get_columns("receivable_credit_decision_operation")
            }
            for name in (
                "contract_ref",
                "contract_guid",
                "contract_organization_guid",
                "expected_current_debt_control_enabled",
                "proposed_debt_control_enabled",
                "readback_debt_control_enabled",
            ):
                assert columns[name]["nullable"]
            legacy = connection.exec_driver_sql(
                "SELECT contract_guid FROM receivable_credit_decision_operation "
                "WHERE bitrix_item_id = 'legacy'"
            ).one()
            assert legacy[0] is None
            contract_module.downgrade()
            assert "contract_guid" not in {
                item["name"]
                for item in inspect(connection).get_columns("receivable_credit_decision_operation")
            }
            base_module.downgrade()
    finally:
        engine.dispose()
