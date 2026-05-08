from functools import lru_cache
from typing import Generator

from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.core.config import get_settings

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


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    settings = get_settings()
    return create_engine(settings.database_url)


def get_db() -> Generator[Session, None, None]:
    engine = get_engine()
    session = Session(engine)
    try:
        yield session
    finally:
        session.close()


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
