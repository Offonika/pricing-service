from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.api.customer_settlements import (
    customer_settlement_eligibility,
    customer_settlement_summary,
)
from app.core.config import Settings
from app.main import app
from app.models import Base
from app.models.customer_settlement import (
    CustomerSettlementAssertionJti,
    CustomerSettlementReconciliationRun,
)
from app.services.customer_settlement_auth import (
    create_customer_settlement_assertion,
)
from app.services.customer_settlement_reconciliation import (
    CustomerSettlementReconciliationResult,
    customer_settlement_reconciliation_context_hash,
    customer_settlement_reconciliation_input_hash,
    end_of_day_boundary_utc,
    latest_customer_settlement_reconciliation,
    store_reconciliation_result,
)
from app.services.customer_settlements import (
    SettlementBalanceInput,
    SettlementMappingInput,
    activate_financial_revision,
    activate_mapping_revision,
    onec_ref_to_guid,
    set_pilot_access,
)

ORG = "0x" + "a" * 32
ORG_GUID = onec_ref_to_guid(ORG)
CP_1 = "0x" + "1" * 32
CP_2 = "0x" + "2" * 32


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        customer_settlements_enabled=True,
        customer_settlements_eligibility_enabled=True,
        customer_settlements_source_validated=True,
        customer_settlements_mapping_mode="crm_readonly",
        customer_settlements_source_mode="synthetic-test",
        customer_settlements_organization_ref=ORG,
        customer_settlements_organization_guid=ORG_GUID,
        customer_settlements_assertion_active_kid="test-key",
        customer_settlements_assertion_active_secret="synthetic-api-test-secret-32bytes",
        customer_settlements_allowed_source_ips=["127.0.0.1/32"],
        customer_settlements_correlation_salt="synthetic-correlation-salt",
    )


def _seed(session: Session, now: datetime) -> None:
    mapping_revision, _ = activate_mapping_revision(
        session,
        entries=[
            SettlementMappingInput("101", "cluster-101", CP_1, "linked"),
            SettlementMappingInput("102", "cluster-102", CP_2, "linked"),
        ],
        source_checked_at=now,
        organization_ref=ORG,
        organization_guid=ORG_GUID,
    )
    activate_financial_revision(
        session,
        organization_ref=ORG,
        as_of=now,
        source_db_time=now,
        source_mode="synthetic-test",
        expected_counterparty_refs=[CP_1, CP_2],
        balances=[
            SettlementBalanceInput(CP_1, Decimal("10.00")),
            SettlementBalanceInput(CP_2, Decimal("20.00")),
        ],
        synced_at=now,
    )
    set_pilot_access(session, site_user_id="101", enabled=True)
    set_pilot_access(session, site_user_id="102", enabled=True)
    report_hash = "a" * 64
    source_hash = "b" * 64
    report_date = (now - timedelta(days=1)).date()
    context_hash = customer_settlement_reconciliation_context_hash(
        mapping_source_hash=mapping_revision.source_hash,
        organization_ref=ORG,
        organization_guid=ORG_GUID,
        source_mode="synthetic-test",
        opening_organization_field="",
        movement_organization_field="",
        counterparty_refs=(CP_1, CP_2),
    )
    store_reconciliation_result(
        session,
        CustomerSettlementReconciliationResult(
            report_date=report_date,
            as_of=end_of_day_boundary_utc(report_date),
            report_hash=report_hash,
            context_hash=context_hash,
            source_hash=source_hash,
            input_hash=customer_settlement_reconciliation_input_hash(
                report_hash=report_hash,
                context_hash=context_hash,
                source_hash=source_hash,
            ),
            status="matched",
            expected_count=2,
            matched_count=2,
            mismatch_count=0,
            max_abs_difference=Decimal("0.00"),
        ),
    )
    session.commit()


def test_api_is_server_scoped_replay_safe_and_never_cacheable(
    monkeypatch,
    caplog,
) -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    settings = _settings()
    now = datetime.now(UTC)
    with Session(engine) as session:
        _seed(session, now)

    monkeypatch.setattr("app.api.customer_settlements.get_settings", lambda: settings)
    monkeypatch.setattr(
        "app.api.customer_settlements.assert_expected_application_database",
        lambda *_args, **_kwargs: None,
    )
    caplog.set_level(logging.INFO, logger="app.customer_settlements")
    issued_at = int(time.time())
    token_101, _ = create_customer_settlement_assertion(
        site_user_id="101",
        settings=settings,
        now=issued_at,
        jti="api_test_user_101_12345",
    )
    token_102, _ = create_customer_settlement_assertion(
        site_user_id="102",
        settings=settings,
        now=issued_at,
        jti="api_test_user_102_12345",
    )

    try:
        request = Request(
            {
                "type": "http",
                "client": ("127.0.0.1", 50000),
                "headers": [],
            }
        )
        unauthorized_response = Response()
        with Session(engine) as session, pytest.raises(HTTPException) as unauthorized:
            customer_settlement_summary(
                request=request,
                response=unauthorized_response,
                credentials=None,
                db=session,
            )
        assert unauthorized.value.status_code == 401
        assert unauthorized.value.headers == {
            "Cache-Control": "private, no-store",
            "Pragma": "no-cache",
        }

        with Session(engine) as session:
            first_response = Response()
            first = customer_settlement_summary(
                request=request,
                response=first_response,
                credentials=HTTPAuthorizationCredentials(
                    scheme="Bearer",
                    credentials=token_101,
                ),
                db=session,
            )
            assert first_response.headers["cache-control"] == "private, no-store"
            assert first_response.headers["pragma"] == "no-cache"
            assert first.amount == Decimal("10.00")

        with Session(engine) as session, pytest.raises(HTTPException) as replay:
            customer_settlement_summary(
                request=request,
                response=Response(),
                credentials=HTTPAuthorizationCredentials(
                    scheme="Bearer",
                    credentials=token_101,
                ),
                db=session,
            )
        assert replay.value.status_code == 401
        assert replay.value.headers == {
            "Cache-Control": "private, no-store",
            "Pragma": "no-cache",
        }

        with Session(engine) as session:
            second = customer_settlement_summary(
                request=request,
                response=Response(),
                credentials=HTTPAuthorizationCredentials(
                    scheme="Bearer",
                    credentials=token_102,
                ),
                db=session,
            )
            assert second.amount == Decimal("20.00")

        operation = app.openapi()["paths"]["/api/customer/settlements/summary"]["get"]
        parameter_names = {parameter["name"] for parameter in operation.get("parameters", [])}
        assert "site_user_id" not in parameter_names
        assert "counterparty_ref" not in parameter_names

        eligibility_token, _ = create_customer_settlement_assertion(
            site_user_id="101",
            settings=settings,
            now=issued_at,
            jti="api_eligibility_101_12345",
        )
        with Session(engine) as session:
            eligibility_response = Response()
            eligibility = customer_settlement_eligibility(
                request=request,
                response=eligibility_response,
                credentials=HTTPAuthorizationCredentials(
                    scheme="Bearer",
                    credentials=eligibility_token,
                ),
                db=session,
            )
            assert eligibility.status == "eligible"
            assert eligibility_response.headers["cache-control"] == "private, no-store"
        eligibility_operation = app.openapi()["paths"]["/api/customer/settlements/eligibility"][
            "get"
        ]
        assert not eligibility_operation.get("parameters")
        settlement_logs = [
            record.getMessage()
            for record in caplog.records
            if record.name == "app.customer_settlements"
        ]
        assert any('"status": "available"' in message for message in settlement_logs)
        assert any('"reason": "replay"' in message for message in settlement_logs)
        assert all("10.00" not in message and "20.00" not in message for message in settlement_logs)
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_api_runtime_database_guard_runs_before_jti_consumption(monkeypatch) -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    settings = _settings()
    monkeypatch.setattr("app.api.customer_settlements.get_settings", lambda: settings)
    token, _ = create_customer_settlement_assertion(
        site_user_id="101",
        settings=settings,
        now=int(time.time()),
        jti="api_runtime_guard_101_12345",
    )
    request = Request(
        {
            "type": "http",
            "client": ("127.0.0.1", 50000),
            "headers": [],
        }
    )

    try:
        with Session(engine) as session, pytest.raises(HTTPException) as blocked:
            customer_settlement_summary(
                request=request,
                response=Response(),
                credentials=HTTPAuthorizationCredentials(
                    scheme="Bearer",
                    credentials=token,
                ),
                db=session,
            )
        assert blocked.value.status_code == 503
        with Session(engine) as session:
            assert (
                session.scalar(select(func.count()).select_from(CustomerSettlementAssertionJti))
                == 0
            )
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_api_hides_balance_when_newest_reconciliation_is_corrupted(monkeypatch) -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    settings = _settings()
    now = datetime.now(UTC)
    with Session(engine) as session:
        _seed(session, now)

    monkeypatch.setattr("app.api.customer_settlements.get_settings", lambda: settings)
    monkeypatch.setattr(
        "app.api.customer_settlements.assert_expected_application_database",
        lambda *_args, **_kwargs: None,
    )
    request = Request(
        {
            "type": "http",
            "client": ("127.0.0.1", 50000),
            "headers": [],
        }
    )

    try:
        valid_token, _ = create_customer_settlement_assertion(
            site_user_id="101",
            settings=settings,
            now=int(time.time()),
            jti="api_valid_reconciliation_101",
        )
        with Session(engine) as session:
            available = customer_settlement_summary(
                request=request,
                response=Response(),
                credentials=HTTPAuthorizationCredentials(
                    scheme="Bearer",
                    credentials=valid_token,
                ),
                db=session,
            )
            assert available.status == "available"
            assert available.amount == Decimal("10.00")

            current = latest_customer_settlement_reconciliation(session)
            assert current is not None
            session.add(
                CustomerSettlementReconciliationRun(
                    report_date=current.report_date,
                    as_of=current.as_of,
                    report_hash="c" * 64,
                    context_hash=current.context_hash,
                    source_hash="d" * 64,
                    input_hash="e" * 64,
                    status="matched",
                    expected_count=2,
                    matched_count=2,
                    mismatch_count=0,
                    max_abs_difference=Decimal("0.00"),
                    created_at=now + timedelta(seconds=1),
                )
            )
            session.commit()

        blocked_token, _ = create_customer_settlement_assertion(
            site_user_id="101",
            settings=settings,
            now=int(time.time()),
            jti="api_corrupted_reconciliation_101",
        )
        with Session(engine) as session:
            blocked = customer_settlement_summary(
                request=request,
                response=Response(),
                credentials=HTTPAuthorizationCredentials(
                    scheme="Bearer",
                    credentials=blocked_token,
                ),
                db=session,
            )
            assert blocked.status == "temporarily_unavailable"
            assert blocked.amount is None
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_api_auth_database_failure_is_never_cacheable(monkeypatch) -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    settings = _settings()
    monkeypatch.setattr("app.api.customer_settlements.get_settings", lambda: settings)

    def fail_database_guard(*_args, **_kwargs):
        raise RuntimeError("synthetic database connection detail")

    monkeypatch.setattr(
        "app.api.customer_settlements.assert_expected_application_database",
        fail_database_guard,
    )
    token, _ = create_customer_settlement_assertion(
        site_user_id="101",
        settings=settings,
        now=int(time.time()),
        jti="api_auth_database_failure_101",
    )
    request = Request(
        {
            "type": "http",
            "client": ("127.0.0.1", 50000),
            "headers": [],
        }
    )

    try:
        with Session(engine) as session, pytest.raises(HTTPException) as failure:
            customer_settlement_summary(
                request=request,
                response=Response(),
                credentials=HTTPAuthorizationCredentials(
                    scheme="Bearer",
                    credentials=token,
                ),
                db=session,
            )
        assert failure.value.status_code == 503
        assert failure.value.detail == "temporarily unavailable"
        assert failure.value.headers == {
            "Cache-Control": "private, no-store",
            "Pragma": "no-cache",
        }
        assert "synthetic database connection detail" not in str(failure.value.detail)
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_api_settings_failure_is_never_cacheable(monkeypatch) -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    def fail_settings():
        raise RuntimeError("synthetic configuration detail")

    monkeypatch.setattr("app.api.customer_settlements.get_settings", fail_settings)
    request = Request(
        {
            "type": "http",
            "client": ("127.0.0.1", 50000),
            "headers": [],
        }
    )

    try:
        with Session(engine) as session, pytest.raises(HTTPException) as failure:
            customer_settlement_summary(
                request=request,
                response=Response(),
                credentials=HTTPAuthorizationCredentials(
                    scheme="Bearer",
                    credentials="synthetic-token",
                ),
                db=session,
            )
        assert failure.value.status_code == 503
        assert failure.value.detail == "temporarily unavailable"
        assert failure.value.headers == {
            "Cache-Control": "private, no-store",
            "Pragma": "no-cache",
        }
        assert "synthetic configuration detail" not in str(failure.value.detail)
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.mark.parametrize(
    ("endpoint_name", "jti"),
    (
        ("summary", "api_summary_failure_101_12345"),
        ("eligibility", "api_eligibility_failure_101_12345"),
    ),
)
def test_api_service_failure_is_never_cacheable(
    monkeypatch,
    endpoint_name: str,
    jti: str,
) -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    settings = _settings()
    monkeypatch.setattr("app.api.customer_settlements.get_settings", lambda: settings)
    monkeypatch.setattr(
        "app.api.customer_settlements.assert_expected_application_database",
        lambda *_args, **_kwargs: None,
    )

    def fail_service(*_args, **_kwargs):
        raise RuntimeError("synthetic sensitive service error")

    monkeypatch.setattr(
        f"app.api.customer_settlements.get_customer_settlement_{endpoint_name}",
        fail_service,
    )
    token, _ = create_customer_settlement_assertion(
        site_user_id="101",
        settings=settings,
        now=int(time.time()),
        jti=jti,
    )
    request = Request(
        {
            "type": "http",
            "client": ("127.0.0.1", 50000),
            "headers": [],
        }
    )
    endpoint = (
        customer_settlement_summary
        if endpoint_name == "summary"
        else customer_settlement_eligibility
    )

    try:
        with Session(engine) as session, pytest.raises(HTTPException) as failure:
            endpoint(
                request=request,
                response=Response(),
                credentials=HTTPAuthorizationCredentials(
                    scheme="Bearer",
                    credentials=token,
                ),
                db=session,
            )
        assert failure.value.status_code == 503
        assert failure.value.detail == "temporarily unavailable"
        assert failure.value.headers == {
            "Cache-Control": "private, no-store",
            "Pragma": "no-cache",
        }
        assert "synthetic sensitive service error" not in str(failure.value.detail)
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()
