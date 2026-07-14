from typing import Generator

from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.infrastructure.db import (
    SqlAlchemyUnitOfWork,
    get_application_engine,
    get_application_session_factory,
)

security = HTTPBearer(auto_error=False)


def _require_bearer_token(
    credentials: HTTPAuthorizationCredentials | None,
    expected: str | None,
    *,
    missing_detail: str = "internal token not configured",
) -> str:
    if not expected:
        raise HTTPException(status_code=401, detail=missing_detail)
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="unauthorized")
    if credentials.credentials != expected:
        raise HTTPException(status_code=401, detail="unauthorized")
    return credentials.credentials


# Compatibility alias retained for API/tests that import the historic symbol.
get_engine = get_application_engine


def get_db() -> Generator[Session, None, None]:
    session = get_application_session_factory()()
    try:
        yield session
    finally:
        session.close()


def get_uow() -> Generator[SqlAlchemyUnitOfWork, None, None]:
    with SqlAlchemyUnitOfWork() as unit_of_work:
        yield unit_of_work


def require_management_internal_token(
    credentials: HTTPAuthorizationCredentials = Security(security),
) -> str:
    settings = get_settings()
    expected = (
        settings.management_internal_api_token
        or settings.counterparty_duplicate_internal_api_token
        or settings.return_scheme_internal_api_token
    )
    return _require_bearer_token(credentials, expected)


def require_weekly_kpi_ingest_token(
    credentials: HTTPAuthorizationCredentials = Security(security),
) -> str:
    settings = get_settings()
    return _require_bearer_token(
        credentials,
        settings.weekly_kpi_ingest_internal_api_token,
        missing_detail="weekly KPI ingest token not configured",
    )


def require_logistics_internal_token(
    credentials: HTTPAuthorizationCredentials = Security(security),
) -> str:
    settings = get_settings()
    return _require_bearer_token(
        credentials,
        settings.logistics_internal_api_token,
        missing_detail="logistics internal token not configured",
    )


def require_expertise_internal_token(
    credentials: HTTPAuthorizationCredentials = Security(security),
) -> str:
    settings = get_settings()
    return _require_bearer_token(
        credentials,
        settings.expertise_internal_api_token,
        missing_detail="expertise internal token not configured",
    )


def require_site_defect_archive_internal_token(
    credentials: HTTPAuthorizationCredentials = Security(security),
) -> str:
    settings = get_settings()
    expected = (
        settings.site_defect_archive_internal_api_token
        or settings.expertise_internal_api_token
        or settings.management_internal_api_token
    )
    return _require_bearer_token(
        credentials,
        expected,
        missing_detail="site defect archive internal token not configured",
    )


def require_card_balance_reconciliation_internal_token(
    credentials: HTTPAuthorizationCredentials = Security(security),
) -> str:
    settings = get_settings()
    expected = (
        settings.card_balance_reconciliation_internal_api_token
        or settings.management_internal_api_token
    )
    return _require_bearer_token(
        credentials,
        expected,
        missing_detail="card balance reconciliation internal token not configured",
    )


def require_bank_payments_internal_token(
    credentials: HTTPAuthorizationCredentials = Security(security),
) -> str:
    settings = get_settings()
    expected = settings.bank_payments_internal_api_token or settings.management_internal_api_token
    return _require_bearer_token(
        credentials,
        expected,
        missing_detail="bank payments internal token not configured",
    )


def require_order_fulfillment_internal_token(
    credentials: HTTPAuthorizationCredentials = Security(security),
) -> str:
    settings = get_settings()
    expected = (
        settings.order_fulfillment_internal_api_token
        or settings.logistics_internal_api_token
        or settings.management_internal_api_token
    )
    return _require_bearer_token(
        credentials,
        expected,
        missing_detail="order fulfillment internal token not configured",
    )
