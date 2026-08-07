from __future__ import annotations

import hashlib
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, Security
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from app.api.dependencies import get_db, security
from app.core.config import get_settings
from app.schemas.customer_settlement import CustomerSettlementSummaryResponse
from app.services.customer_settlement_auth import (
    CustomerSettlementAuthConfigError,
    CustomerSettlementAuthError,
    verify_and_consume_customer_settlement_assertion,
)
from app.services.customer_settlements import get_customer_settlement_summary

router = APIRouter()
logger = logging.getLogger("app.customer_settlements")
_NO_STORE_HEADERS = {
    "Cache-Control": "private, no-store",
    "Pragma": "no-cache",
}


def _correlation_hash(value: str, salt: str | None) -> str | None:
    if not salt:
        return None
    return hashlib.sha256(f"{salt}:{value}".encode()).hexdigest()[:16]


def _log_event(event: str, **fields: object) -> None:
    payload = {"event": event, **{key: value for key, value in fields.items() if value is not None}}
    logger.info(json.dumps(payload, ensure_ascii=True, sort_keys=True))


@router.get(
    "/api/customer/settlements/summary",
    response_model=CustomerSettlementSummaryResponse,
    response_model_exclude_none=True,
)
def customer_settlement_summary(
    request: Request,
    response: Response,
    credentials: HTTPAuthorizationCredentials | None = Security(security),
    db: Session = Depends(get_db),
) -> CustomerSettlementSummaryResponse:
    response.headers.update(_NO_STORE_HEADERS)
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=401,
            detail="unauthorized",
            headers=_NO_STORE_HEADERS,
        )
    settings = get_settings()
    try:
        identity = verify_and_consume_customer_settlement_assertion(
            db,
            token=credentials.credentials,
            source_ip=request.client.host if request.client else None,
            settings=settings,
        )
        db.commit()
    except CustomerSettlementAuthError as exc:
        db.rollback()
        _log_event("customer_settlement_auth_failure", reason=exc.code)
        raise HTTPException(
            status_code=401,
            detail="unauthorized",
            headers=_NO_STORE_HEADERS,
        ) from exc
    except (CustomerSettlementAuthConfigError, ValueError) as exc:
        db.rollback()
        _log_event(
            "customer_settlement_auth_config_failure",
            reason=type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="temporarily unavailable",
            headers=_NO_STORE_HEADERS,
        ) from exc

    summary = get_customer_settlement_summary(
        db,
        site_user_id=identity.site_user_id,
        enabled=settings.customer_settlements_enabled,
        stale_after_seconds=settings.customer_settlements_stale_after_seconds,
        hide_after_seconds=settings.customer_settlements_hide_after_seconds,
        mapping_stale_after_seconds=settings.customer_settlements_mapping_stale_after_seconds,
    )
    _log_event(
        "customer_settlement_summary",
        status=summary.status,
        user_hash=_correlation_hash(
            identity.site_user_id,
            settings.customer_settlements_correlation_salt,
        ),
    )
    return CustomerSettlementSummaryResponse(**summary.__dict__)
