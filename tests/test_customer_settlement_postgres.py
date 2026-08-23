from __future__ import annotations

import importlib.util
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from threading import Barrier, Event
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.core.config import Settings
from app.models.customer_settlement import (
    CustomerSettlementAlertOutbox,
    CustomerSettlementAssertionJti,
    CustomerSettlementReconciliationRun,
    CustomerSettlementRevision,
)
from app.services import customer_settlement_alerts as settlement_alerts
from app.services.customer_settlement_auth import (
    CustomerSettlementAuthError,
    create_customer_settlement_assertion,
    verify_and_consume_customer_settlement_assertion,
)
from app.services.customer_settlements import (
    CustomerSettlementRuntimeGuardError,
    SettlementBalanceInput,
    activate_financial_revision,
    assert_expected_application_database,
    cleanup_customer_settlements,
    mark_financial_revision_failed,
)
from app.workers.customer_settlements import _advisory_lock

POSTGRES_URL_ENV = "CUSTOMER_SETTLEMENTS_TEST_POSTGRES_URL"
BASE_TIME = datetime(2026, 7, 30, 9, 0, tzinfo=UTC)
ORG = "0x" + "a" * 32
CP_1 = "0x" + "1" * 32
SETTLEMENT_TABLES = {
    "customer_settlement_revision",
    "customer_settlement_balance",
    "customer_settlement_mapping_revision",
    "customer_settlement_mapping_entry",
    "customer_settlement_pilot_access",
    "customer_settlement_assertion_jti",
    "customer_account",
    "customer_account_site_binding",
    "customer_account_source_binding",
    "customer_settlement_reconciliation_run",
    "customer_settlement_alert_state",
    "customer_settlement_alert_outbox",
}


def _load_migration(filename: str, module_name: str):
    path = Path(__file__).resolve().parents[1] / "alembic/versions" / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _load_settlement_migrations():
    base = _load_migration(
        "c3d4e5f6a7b9_add_customer_settlements.py",
        "customer_settlement_postgres_base_migration",
    )
    accounts = _load_migration(
        "d9e1f3a5b7c9_add_customer_account_guid_mapping.py",
        "customer_settlement_postgres_account_migration",
    )
    operational = _load_migration(
        "4c6e8a0b2d3f_add_settlement_reconciliation_alerts.py",
        "customer_settlement_postgres_operational_migration",
    )
    reconciliation_context = _load_migration(
        "6e8f0a2b4c6d_bind_settlement_reconciliation_context.py",
        "customer_settlement_postgres_reconciliation_context_migration",
    )
    return base, accounts, operational, reconciliation_context


@pytest.fixture(scope="module")
def postgres_engine():
    database_url = os.getenv(POSTGRES_URL_ENV)
    if not database_url:
        pytest.skip(f"{POSTGRES_URL_ENV} is not configured")
    if not database_url.startswith(("postgresql://", "postgresql+psycopg2://")):
        pytest.fail(f"{POSTGRES_URL_ENV} must point to PostgreSQL")

    administration_engine = create_engine(database_url, poolclass=NullPool)
    schema = f"customer_settlement_test_{uuid4().hex}"
    with administration_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))

    engine = create_engine(
        database_url,
        connect_args={"options": f"-csearch_path={schema}"},
        poolclass=NullPool,
    )
    (
        base_migration,
        account_migration,
        operational_migration,
        reconciliation_context_migration,
    ) = _load_settlement_migrations()
    try:
        with engine.begin() as connection:
            base_migration.op = Operations(MigrationContext.configure(connection))
            base_migration.upgrade()
            account_migration.op = Operations(MigrationContext.configure(connection))
            account_migration.upgrade()
            operational_migration.op = Operations(MigrationContext.configure(connection))
            operational_migration.upgrade()
            reconciliation_context_migration.op = Operations(MigrationContext.configure(connection))
            reconciliation_context_migration.upgrade()
        yield engine
    finally:
        engine.dispose()
        with administration_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        administration_engine.dispose()


def _activate(
    session: Session,
    *,
    amount: str,
    as_of: datetime,
) -> CustomerSettlementRevision:
    revision, _ = activate_financial_revision(
        session,
        organization_ref=ORG,
        as_of=as_of,
        source_db_time=as_of,
        source_mode="postgres-integration-test",
        expected_counterparty_refs=[CP_1],
        balances=[
            SettlementBalanceInput(
                counterparty_ref=CP_1,
                signed_balance=amount,
            )
        ],
        synced_at=as_of,
    )
    return revision


def _assertion_settings() -> Settings:
    return Settings(
        _env_file=None,
        customer_settlements_assertion_issuer="master-mobile.ru",
        customer_settlements_assertion_audience="pricing-service:customer-settlements",
        customer_settlements_assertion_active_kid="postgres-test-key",
        customer_settlements_assertion_active_secret="synthetic-postgres-test-secret",
        customer_settlements_assertion_ttl_seconds=60,
        customer_settlements_assertion_clock_skew_seconds=30,
        customer_settlements_allowed_source_ips=["127.0.0.1/32"],
    )


def test_postgres_migration_supports_upgrade_and_downgrade(postgres_engine) -> None:
    schema = f"customer_settlement_migration_{uuid4().hex}"
    database_url = os.environ[POSTGRES_URL_ENV]
    administration_engine = create_engine(database_url, poolclass=NullPool)
    with administration_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        database_url,
        connect_args={"options": f"-csearch_path={schema}"},
        poolclass=NullPool,
    )
    (
        base_migration,
        account_migration,
        operational_migration,
        reconciliation_context_migration,
    ) = _load_settlement_migrations()
    operational_tables = {
        "customer_settlement_reconciliation_run",
        "customer_settlement_alert_state",
        "customer_settlement_alert_outbox",
    }
    base_tables = (
        SETTLEMENT_TABLES
        - {
            "customer_account",
            "customer_account_site_binding",
            "customer_account_source_binding",
        }
        - operational_tables
    )
    try:
        with engine.begin() as connection:
            base_migration.op = Operations(MigrationContext.configure(connection))
            base_migration.upgrade()
            account_migration.op = Operations(MigrationContext.configure(connection))
            account_migration.upgrade()
            operational_migration.op = Operations(MigrationContext.configure(connection))
            operational_migration.upgrade()
            reconciliation_context_migration.op = Operations(MigrationContext.configure(connection))
            reconciliation_context_migration.upgrade()
            assert SETTLEMENT_TABLES.issubset(inspect(connection).get_table_names())
        with engine.begin() as connection:
            reconciliation_context_migration.op = Operations(MigrationContext.configure(connection))
            reconciliation_context_migration.downgrade()
            operational_migration.op = Operations(MigrationContext.configure(connection))
            operational_migration.downgrade()
            assert operational_tables.isdisjoint(inspect(connection).get_table_names())
            account_migration.op = Operations(MigrationContext.configure(connection))
            account_migration.downgrade()
            assert base_tables.issubset(inspect(connection).get_table_names())
            assert (SETTLEMENT_TABLES - base_tables).isdisjoint(
                inspect(connection).get_table_names()
            )
            base_migration.op = Operations(MigrationContext.configure(connection))
            base_migration.downgrade()
            assert SETTLEMENT_TABLES.isdisjoint(inspect(connection).get_table_names())
    finally:
        engine.dispose()
        with administration_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        administration_engine.dispose()


def test_postgres_runtime_database_guard_checks_actual_database(postgres_engine) -> None:
    with Session(postgres_engine) as session:
        actual_database = session.scalar(text("SELECT current_database()"))
        assert isinstance(actual_database, str)
        assert_expected_application_database(
            session,
            expected_database_name=actual_database,
        )
        with pytest.raises(CustomerSettlementRuntimeGuardError):
            assert_expected_application_database(
                session,
                expected_database_name=f"{actual_database}_wrong",
            )


def test_postgres_partial_unique_index_and_atomic_activation_rollback(
    postgres_engine,
) -> None:
    with Session(postgres_engine) as session:
        active = _activate(session, amount="10.00", as_of=BASE_TIME)
        session.commit()
        active_id = active.id

    with Session(postgres_engine) as session:
        try:
            _activate(
                session,
                amount="20.00",
                as_of=BASE_TIME + timedelta(hours=1),
            )
            raise RuntimeError("synthetic failure before commit")
        except RuntimeError:
            session.rollback()

    with Session(postgres_engine) as session:
        active_rows = list(
            session.scalars(
                select(CustomerSettlementRevision).where(
                    CustomerSettlementRevision.status == "active"
                )
            )
        )
        assert [row.id for row in active_rows] == [active_id]
        duplicate_active = CustomerSettlementRevision(
            status="active",
            organization_ref=ORG,
            currency="RUB",
            as_of=BASE_TIME + timedelta(hours=2),
            source_db_time=BASE_TIME + timedelta(hours=2),
            synced_at=BASE_TIME + timedelta(hours=2),
            source_mode="postgres-integration-test",
            source_hash="f" * 64,
            expected_row_count=0,
            loaded_row_count=0,
            zero_row_count=0,
        )
        session.add(duplicate_active)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_postgres_advisory_lock_excludes_second_worker(postgres_engine) -> None:
    with Session(postgres_engine) as first, Session(postgres_engine) as second:
        with _advisory_lock(first, "customer-settlements:postgres-integration") as acquired:
            assert acquired is True
            with _advisory_lock(
                second, "customer-settlements:postgres-integration"
            ) as second_acquired:
                assert second_acquired is False
        with _advisory_lock(
            second, "customer-settlements:postgres-integration"
        ) as acquired_after_release:
            assert acquired_after_release is True


def test_postgres_concurrent_jti_consumption_allows_one_request(postgres_engine) -> None:
    settings = _assertion_settings()
    token, _ = create_customer_settlement_assertion(
        site_user_id="901",
        settings=settings,
        now=1_785_412_800,
        jti=f"postgres_concurrent_{uuid4().hex}",
    )
    barrier = Barrier(2)

    def consume() -> str:
        with Session(postgres_engine) as session:
            barrier.wait()
            try:
                verify_and_consume_customer_settlement_assertion(
                    session,
                    token=token,
                    source_ip="127.0.0.1",
                    settings=settings,
                    now=1_785_412_801,
                )
                session.commit()
                return "accepted"
            except CustomerSettlementAuthError as exc:
                session.rollback()
                return exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: consume(), range(2)))

    assert sorted(outcomes) == ["accepted", "replay"]


def test_postgres_alert_dispatch_claims_each_outbox_row_once(
    postgres_engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = Event()
    release = Event()
    with Session(postgres_engine) as session:
        session.add(
            CustomerSettlementAlertOutbox(
                event_key=f"postgres-alert-{uuid4().hex}",
                status="pending",
                severity="critical",
                message="synthetic safe alert",
                attempt_count=0,
                next_attempt_at=BASE_TIME,
            )
        )
        session.commit()

    def fake_post(**_: object) -> str:
        started.set()
        if not release.wait(timeout=10):
            raise RuntimeError("synthetic_dispatch_timeout")
        return "synthetic-comment"

    monkeypatch.setattr(settlement_alerts, "_post_bitrix_comment", fake_post)

    def dispatch_first() -> dict[str, int]:
        with Session(postgres_engine) as session:
            result = settlement_alerts.dispatch_customer_settlement_alerts(
                session,
                webhook_url="https://example.invalid/rest/1/token",
                task_id="2883",
                now=BASE_TIME,
            )
            session.commit()
            return result

    with ThreadPoolExecutor(max_workers=1) as executor:
        first_future = executor.submit(dispatch_first)
        assert started.wait(timeout=5)
        try:
            with Session(postgres_engine) as session:
                second = settlement_alerts.dispatch_customer_settlement_alerts(
                    session,
                    webhook_url="https://example.invalid/rest/1/token",
                    task_id="2883",
                    now=BASE_TIME,
                )
                session.commit()
            assert second == {"processed": 0, "sent": 0, "failed": 0, "exhausted": 0}
        finally:
            release.set()
        first = first_future.result(timeout=10)

    assert first == {"processed": 1, "sent": 1, "failed": 0, "exhausted": 0}


def test_postgres_retention_never_deletes_active_revision(postgres_engine) -> None:
    old_time = BASE_TIME - timedelta(days=40)
    with Session(postgres_engine) as session:
        old_revision = _activate(session, amount="30.00", as_of=old_time)
        active_revision = _activate(session, amount="40.00", as_of=BASE_TIME)
        failed_revision = mark_financial_revision_failed(
            session,
            organization_ref=ORG,
            as_of=old_time,
            source_mode="postgres-integration-test",
            error_code="synthetic",
        )
        old_revision.created_at = old_time
        active_revision.created_at = old_time
        failed_revision.created_at = old_time
        session.add(
            CustomerSettlementAssertionJti(
                jti_hash="e" * 64,
                expires_at=BASE_TIME - timedelta(hours=25),
                consumed_at=old_time,
            )
        )
        session.add_all(
            [
                CustomerSettlementReconciliationRun(
                    report_date=date(2026, 6, 20),
                    as_of=old_time,
                    report_hash="d" * 64,
                    status="matched",
                    expected_count=1,
                    matched_count=1,
                    mismatch_count=0,
                    max_abs_difference="0.00",
                    created_at=old_time,
                ),
                CustomerSettlementAlertOutbox(
                    event_key="postgres-retention-alert",
                    status="sent",
                    severity="warning",
                    message="synthetic safe alert",
                    attempt_count=1,
                    next_attempt_at=old_time,
                    sent_at=old_time,
                    created_at=old_time,
                    updated_at=old_time,
                ),
            ]
        )
        session.commit()
        active_id = active_revision.id

    with Session(postgres_engine) as session:
        result = cleanup_customer_settlements(
            session,
            successful_retention_days=30,
            failed_retention_days=7,
            jti_retention_hours=24,
            now=BASE_TIME,
        )
        session.commit()

    with Session(postgres_engine) as session:
        active = session.get(CustomerSettlementRevision, active_id)
        assert active is not None
        assert active.status == "active"
        assert result["financial_revisions"] == 2
        assert result["assertion_jti"] == 1
        assert result["reconciliation_runs"] == 1
        assert result["alert_outbox"] == 1
