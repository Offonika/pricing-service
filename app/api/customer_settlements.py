from __future__ import annotations

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
_NO_STORE_HEADERS = {
    "Cache-Control": "private, no-store",
    "Pragma": "no-cache",
}


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
        raise HTTPException(
            status_code=401,
            detail="unauthorized",
            headers=_NO_STORE_HEADERS,
        ) from exc
    except (CustomerSettlementAuthConfigError, ValueError) as exc:
        db.rollback()
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
    return CustomerSettlementSummaryResponse(**summary.__dict__)
