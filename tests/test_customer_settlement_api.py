from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi import HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.customer_settlements import (
    customer_settlement_eligibility,
    customer_settlement_summary,
)
from app.core.config import Settings
from app.main import app
from app.models import Base
from app.services.customer_settlement_auth import (
    create_customer_settlement_assertion,
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
        customer_settlements_assertion_active_kid="test-key",
        customer_settlements_assertion_active_secret="synthetic-test-secret",
        customer_settlements_allowed_source_ips=["127.0.0.1/32"],
        customer_settlements_correlation_salt="synthetic-correlation-salt",
    )


def _seed(session: Session, now: datetime) -> None:
    activate_mapping_revision(
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
