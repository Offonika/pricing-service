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
        "contract_ref": "0X8FDA0025901E48EE11ED222EA7D9B22F",
        "contract_guid": "a7d9b22f-222e-11ed-8fda-0025901e48ee",
        "contract_code": "РБ0058149",
        "contract_name": "Основной договор1",
        "contract_organization_ref": "0X8FDA0025901E48EE11ED222EA7D9B230",
        "contract_organization_guid": "a7d9b230-222e-11ed-8fda-0025901e48ee",
        "expected_current_limit": Decimal("100000"),
        "expected_current_depth": 7,
        "expected_control_enabled": True,
        "proposed_limit": Decimal("150000"),
        "proposed_depth": 14,
        "proposed_control_enabled": True,
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
