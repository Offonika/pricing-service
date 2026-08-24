from __future__ import annotations

import hashlib
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, Security
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from app.api.dependencies import get_db, security
from app.core.config import get_settings
from app.schemas.customer_settlement import (
    CustomerSettlementEligibilityResponse,
    CustomerSettlementSummaryResponse,
)
from app.services.customer_settlement_auth import (
    CustomerSettlementAuthConfigError,
    CustomerSettlementAuthError,
    verify_and_consume_customer_settlement_assertion,
)
from app.services.customer_settlement_reconciliation import (
    active_customer_settlement_reconciliation_is_current,
)
from app.services.customer_settlements import (
    CustomerSettlementRuntimeGuardError,
    assert_expected_application_database,
    get_customer_settlement_eligibility,
    get_customer_settlement_summary,
    validate_customer_settlement_freshness_contract,
)

router = APIRouter()
logger = logging.getLogger("app.customer_settlements")
_NO_STORE_HEADERS = {
    "Cache-Control": "private, no-store",
    "Pragma": "no-cache",
}
_MAPPING_SOURCE_BY_MODE = {
    "crm_readonly": "bitrix_crm_customer_cluster",
    "manual_confirmed": "manual_confirmed_pilot",
}


def _correlation_hash(value: str, salt: str | None) -> str | None:
    if not salt:
        return None
    return hashlib.sha256(f"{salt}:{value}".encode()).hexdigest()[:16]


def _log_event(event: str, **fields: object) -> None:
    payload = {"event": event, **{key: value for key, value in fields.items() if value is not None}}
    logger.info(json.dumps(payload, ensure_ascii=True, sort_keys=True))


def _rollback_quietly(db: Session) -> None:
    try:
        db.rollback()
    except Exception:
        logger.warning("customer settlement database rollback failed", exc_info=False)


def _authenticate_customer_request(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
    db: Session,
):
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=401,
            detail="unauthorized",
            headers=_NO_STORE_HEADERS,
        )
    try:
        settings = get_settings()
        validate_customer_settlement_freshness_contract(
            stale_after_seconds=settings.customer_settlements_stale_after_seconds,
            hide_after_seconds=settings.customer_settlements_hide_after_seconds,
            mapping_stale_after_seconds=(settings.customer_settlements_mapping_stale_after_seconds),
        )
        assert_expected_application_database(
            db,
            expected_database_name=settings.customer_settlements_expected_database_name,
        )
        identity = verify_and_consume_customer_settlement_assertion(
            db,
            token=credentials.credentials,
            source_ip=request.client.host if request.client else None,
            settings=settings,
        )
        db.commit()
        return identity, settings
    except CustomerSettlementAuthError as exc:
        _rollback_quietly(db)
        _log_event("customer_settlement_auth_failure", reason=exc.code)
        raise HTTPException(
            status_code=401,
            detail="unauthorized",
            headers=_NO_STORE_HEADERS,
        ) from exc
    except (
        CustomerSettlementAuthConfigError,
        CustomerSettlementRuntimeGuardError,
        ValueError,
    ) as exc:
        _rollback_quietly(db)
        _log_event("customer_settlement_auth_config_failure", reason=type(exc).__name__)
        raise HTTPException(
            status_code=503,
            detail="temporarily unavailable",
            headers=_NO_STORE_HEADERS,
        ) from exc
    except Exception as exc:
        _rollback_quietly(db)
        _log_event("customer_settlement_auth_service_failure", reason=type(exc).__name__)
        raise HTTPException(
            status_code=503,
            detail="temporarily unavailable",
            headers=_NO_STORE_HEADERS,
        ) from exc


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
    identity, settings = _authenticate_customer_request(request, credentials, db)

    try:
        reconciliation_validated = (
            settings.customer_settlements_source_validated
            and active_customer_settlement_reconciliation_is_current(
                db,
                organization_ref=str(settings.customer_settlements_organization_ref or ""),
                organization_guid=str(settings.customer_settlements_organization_guid or ""),
                source_mode=settings.customer_settlements_source_mode,
                opening_organization_field=str(
                    settings.customer_settlements_opening_organization_field or ""
                ),
                movement_organization_field=str(
                    settings.customer_settlements_movement_organization_field or ""
                ),
                max_scope_users=settings.customer_settlements_max_scope_users,
            )
        )
        summary = get_customer_settlement_summary(
            db,
            site_user_id=identity.site_user_id,
            enabled=settings.customer_settlements_enabled,
            source_validated=settings.customer_settlements_source_validated,
            reconciliation_validated=reconciliation_validated,
            stale_after_seconds=settings.customer_settlements_stale_after_seconds,
            hide_after_seconds=settings.customer_settlements_hide_after_seconds,
            mapping_stale_after_seconds=settings.customer_settlements_mapping_stale_after_seconds,
            expected_source_mode=settings.customer_settlements_source_mode,
            expected_mapping_source_name=_MAPPING_SOURCE_BY_MODE.get(
                settings.customer_settlements_mapping_mode,
                "",
            ),
            expected_source_system="ut103",
            expected_organization_ref=str(settings.customer_settlements_organization_ref or ""),
            expected_organization_guid=str(settings.customer_settlements_organization_guid or ""),
            max_scope_users=settings.customer_settlements_max_scope_users,
        )
    except Exception as exc:
        _rollback_quietly(db)
        _log_event("customer_settlement_summary_failure", reason=type(exc).__name__)
        raise HTTPException(
            status_code=503,
            detail="temporarily unavailable",
            headers=_NO_STORE_HEADERS,
        ) from exc
    _log_event(
        "customer_settlement_summary",
        status=summary.status,
        user_hash=_correlation_hash(
            identity.site_user_id,
            settings.customer_settlements_correlation_salt,
        ),
    )
    return CustomerSettlementSummaryResponse(**summary.__dict__)


@router.get(
    "/api/customer/settlements/eligibility",
    response_model=CustomerSettlementEligibilityResponse,
)
def customer_settlement_eligibility(
    request: Request,
    response: Response,
    credentials: HTTPAuthorizationCredentials | None = Security(security),
    db: Session = Depends(get_db),
) -> CustomerSettlementEligibilityResponse:
    response.headers.update(_NO_STORE_HEADERS)
    identity, settings = _authenticate_customer_request(request, credentials, db)
    try:
        reconciliation_validated = (
            settings.customer_settlements_source_validated
            and active_customer_settlement_reconciliation_is_current(
                db,
                organization_ref=str(settings.customer_settlements_organization_ref or ""),
                organization_guid=str(settings.customer_settlements_organization_guid or ""),
                source_mode=settings.customer_settlements_source_mode,
                opening_organization_field=str(
                    settings.customer_settlements_opening_organization_field or ""
                ),
                movement_organization_field=str(
                    settings.customer_settlements_movement_organization_field or ""
                ),
                max_scope_users=settings.customer_settlements_max_scope_users,
            )
        )
        status = get_customer_settlement_eligibility(
            db,
            site_user_id=identity.site_user_id,
            enabled=settings.customer_settlements_eligibility_enabled,
            source_validated=settings.customer_settlements_source_validated,
            reconciliation_validated=reconciliation_validated,
            mapping_stale_after_seconds=settings.customer_settlements_mapping_stale_after_seconds,
            expected_mapping_source_name=_MAPPING_SOURCE_BY_MODE.get(
                settings.customer_settlements_mapping_mode,
                "",
            ),
            expected_source_system="ut103",
            expected_organization_ref=str(settings.customer_settlements_organization_ref or ""),
            expected_organization_guid=str(settings.customer_settlements_organization_guid or ""),
            max_scope_users=settings.customer_settlements_max_scope_users,
        )
    except Exception as exc:
        _rollback_quietly(db)
        _log_event("customer_settlement_eligibility_failure", reason=type(exc).__name__)
        raise HTTPException(
            status_code=503,
            detail="temporarily unavailable",
            headers=_NO_STORE_HEADERS,
        ) from exc
    _log_event(
        "customer_settlement_eligibility",
        status=status,
        user_hash=_correlation_hash(
            identity.site_user_id,
            settings.customer_settlements_correlation_salt,
        ),
    )
    return CustomerSettlementEligibilityResponse(status=status)
