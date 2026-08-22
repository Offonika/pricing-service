from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.dependencies import (
    get_db,
    get_site_service_request_settings,
    require_site_service_request_signature,
)
from app.core.config import Settings
from app.schemas.site_service_requests import (
    SiteServiceRequestEventAcceptedResponse,
    SiteServiceRequestEventPayload,
    SiteServiceRequestHealthResponse,
)
from app.services.site_service_requests import (
    SiteServiceRequestConfigurationError,
    SiteServiceRequestConflictError,
    accept_site_service_request_event,
    build_site_service_request_cipher,
    build_site_service_request_health,
)
from app.services.site_service_requests_auth import VerifiedSiteRequest

router = APIRouter()


@router.post(
    "/events",
    response_model=SiteServiceRequestEventAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        401: {"description": "Invalid or missing site HMAC authentication"},
        409: {"description": "Nonce replay or idempotency conflict"},
        503: {"description": "Ingest, encryption, or storage is unavailable"},
    },
)
def accept_event(
    payload: SiteServiceRequestEventPayload,
    verified: VerifiedSiteRequest = Depends(require_site_service_request_signature),
    settings: Settings = Depends(get_site_service_request_settings),
    db: Session = Depends(get_db),
) -> SiteServiceRequestEventAcceptedResponse:
    if not settings.site_service_requests_ingest_enabled:
        raise HTTPException(status_code=503, detail="ingest_disabled")
    try:
        result = accept_site_service_request_event(
            db,
            payload=payload,
            raw_body=verified.body,
            payload_sha256=verified.content_sha256,
            cipher=build_site_service_request_cipher(settings),
            max_file_bytes=settings.site_service_requests_max_file_bytes,
        )
        db.commit()
    except SiteServiceRequestConflictError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=exc.code) from exc
    except SiteServiceRequestConfigurationError as exc:
        db.rollback()
        raise HTTPException(
            status_code=503,
            detail="event_encryption_not_configured",
        ) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="event_storage_unavailable") from exc

    return SiteServiceRequestEventAcceptedResponse(
        event_id=result.event_id,
        status="accepted",
        duplicate=result.duplicate,
        missing_file_ids=result.missing_file_ids,
    )


@router.get(
    "/health",
    response_model=SiteServiceRequestHealthResponse,
    responses={
        401: {"description": "Invalid or missing site HMAC authentication"},
        409: {"description": "Nonce replay"},
        503: {"description": "Health storage is unavailable"},
    },
)
def health(
    _verified: VerifiedSiteRequest = Depends(require_site_service_request_signature),
    settings: Settings = Depends(get_site_service_request_settings),
    db: Session = Depends(get_db),
) -> SiteServiceRequestHealthResponse:
    try:
        payload = build_site_service_request_health(db, settings=settings)
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="health_unavailable") from exc
    return SiteServiceRequestHealthResponse.model_validate(payload)
