from __future__ import annotations

from email.message import Message
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.dependencies import (
    get_db,
    get_site_service_request_settings,
    require_site_service_request_signature,
)
from app.core.config import Settings
from app.schemas.site_service_requests import (
    SiteServiceEmailEventPayload,
    SiteServiceRequestCommandAckPayload,
    SiteServiceRequestCommandAckResponse,
    SiteServiceRequestCommandPayload,
    SiteServiceRequestCommandsResponse,
    SiteServiceRequestEventAcceptedResponse,
    SiteServiceRequestEventPayload,
    SiteServiceRequestFileStagedResponse,
    SiteServiceRequestHealthResponse,
)
from app.services.site_service_requests import (
    SiteServiceRequestConfigurationError,
    SiteServiceRequestConflictError,
    SiteServiceRequestNotFoundError,
    SiteServiceRequestPayloadError,
    SiteServiceRequestStorageError,
    accept_site_service_email_event,
    accept_site_service_request_event,
    acknowledge_site_service_request_command,
    build_site_service_request_cipher,
    build_site_service_request_health,
    cleanup_unreferenced_site_service_request_file,
    fail_site_service_request_file,
    lease_site_service_request_commands,
    stage_site_service_request_file,
)
from app.services.site_service_requests_auth import VerifiedSiteRequest

router = APIRouter()


@router.post(
    "/email-events",
    response_model=SiteServiceRequestEventAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        401: {"description": "Invalid or missing dispatcher HMAC authentication"},
        409: {"description": "Nonce replay or idempotency conflict"},
        413: {"description": "Request body exceeds the configured limit"},
        503: {"description": "Email ingest, encryption, or storage is unavailable"},
    },
)
def accept_email_event(
    payload: SiteServiceEmailEventPayload,
    verified: VerifiedSiteRequest = Depends(require_site_service_request_signature),
    settings: Settings = Depends(get_site_service_request_settings),
    db: Session = Depends(get_db),
) -> SiteServiceRequestEventAcceptedResponse:
    if not settings.site_service_requests_email_ingest_enabled:
        raise HTTPException(status_code=503, detail="email_ingest_disabled")
    try:
        result = accept_site_service_email_event(
            db,
            payload=payload,
            raw_body=verified.body,
            payload_sha256=verified.content_sha256,
            cipher=build_site_service_request_cipher(settings),
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
        missing_file_ids=[],
    )


@router.post(
    "/events",
    response_model=SiteServiceRequestEventAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        401: {"description": "Invalid or missing site HMAC authentication"},
        409: {"description": "Nonce replay or idempotency conflict"},
        413: {"description": "Request body exceeds the configured limit"},
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


@router.put(
    "/events/{event_id}/files/{file_id}",
    response_model=SiteServiceRequestFileStagedResponse,
    response_model_exclude_none=True,
    responses={
        401: {"description": "Invalid or missing site HMAC authentication"},
        404: {"description": "Event or file metadata is not registered"},
        409: {"description": "Nonce replay or file metadata conflict"},
        413: {"description": "File exceeds the configured size limit"},
        422: {"description": "File body or metadata is invalid"},
        503: {"description": "Ingest or file storage is unavailable"},
    },
)
def upload_event_file(
    event_id: str,
    file_id: int,
    verified: VerifiedSiteRequest = Depends(require_site_service_request_signature),
    settings: Settings = Depends(get_site_service_request_settings),
    db: Session = Depends(get_db),
    content_disposition: Annotated[
        str | None,
        Header(alias="Content-Disposition"),
    ] = None,
    content_type: Annotated[str | None, Header(alias="Content-Type")] = None,
    content_length: Annotated[int | None, Header(alias="Content-Length", ge=0)] = None,
    file_error: Annotated[
        Literal["file_unavailable"] | None,
        Header(alias="X-MM-Site-File-Error"),
    ] = None,
) -> SiteServiceRequestFileStagedResponse:
    if not settings.site_service_requests_ingest_enabled:
        raise HTTPException(status_code=503, detail="ingest_disabled")
    if file_error is None:
        filename = _filename_from_content_disposition(content_disposition)
        if content_type is None or content_length is None:
            raise HTTPException(status_code=422, detail="file_metadata_invalid")
    else:
        filename = ""

    result = None
    try:
        if file_error is not None:
            if verified.body:
                raise SiteServiceRequestPayloadError("file_error_body_invalid")
            result = fail_site_service_request_file(
                db,
                event_id=event_id,
                file_id=file_id,
                error_code=file_error,
            )
        else:
            assert content_type is not None
            assert content_length is not None
            result = stage_site_service_request_file(
                db,
                event_id=event_id,
                file_id=file_id,
                body=verified.body,
                safe_filename=filename,
                mime_type=content_type,
                declared_size=content_length,
                body_sha256=verified.content_sha256,
                spool_dir=settings.site_service_requests_file_spool_dir,
                max_file_bytes=settings.site_service_requests_max_file_bytes,
            )
        db.commit()
    except SiteServiceRequestNotFoundError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=exc.code) from exc
    except SiteServiceRequestPayloadError as exc:
        if exc.persist_state:
            _commit_site_service_request_file_failure(db)
        else:
            db.rollback()
        code = 413 if exc.code == "file_too_large" else 422
        raise HTTPException(status_code=code, detail=exc.code) from exc
    except SiteServiceRequestConflictError as exc:
        if exc.persist_state:
            _commit_site_service_request_file_failure(db)
        else:
            db.rollback()
        raise HTTPException(status_code=409, detail=exc.code) from exc
    except SiteServiceRequestStorageError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="file_storage_unavailable") from exc
    except SQLAlchemyError as exc:
        db.rollback()
        if result is not None:
            try:
                cleanup_unreferenced_site_service_request_file(db, result=result)
            except SQLAlchemyError:
                # A second readback failure is ambiguous. Keep the private spool
                # payload for a later operator/worker reconciliation.
                pass
            finally:
                # Release the identity locks only after the guarded unlink has
                # either completed or failed safe.
                db.rollback()
        raise HTTPException(status_code=503, detail="file_storage_unavailable") from exc

    return SiteServiceRequestFileStagedResponse(
        event_id=result.event_id,
        file_id=result.file_id,
        status=result.status,
        duplicate=result.duplicate,
        error_code=result.error_code,
    )


@router.get(
    "/commands",
    response_model=SiteServiceRequestCommandsResponse,
    responses={
        401: {"description": "Invalid or missing site HMAC authentication"},
        409: {"description": "Nonce replay"},
        503: {"description": "Command storage or encryption is unavailable"},
    },
)
def get_commands(
    _verified: VerifiedSiteRequest = Depends(require_site_service_request_signature),
    settings: Settings = Depends(get_site_service_request_settings),
    db: Session = Depends(get_db),
) -> SiteServiceRequestCommandsResponse:
    if not settings.site_service_requests_outbound_replies_enabled:
        return SiteServiceRequestCommandsResponse(commands=[])
    try:
        commands = lease_site_service_request_commands(
            db,
            cipher=build_site_service_request_cipher(settings),
            enabled=True,
            lease_seconds=settings.site_service_requests_command_lease_seconds,
        )
        db.commit()
    except SiteServiceRequestConfigurationError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="command_encryption_unavailable") from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="command_storage_unavailable") from exc

    return SiteServiceRequestCommandsResponse(
        commands=[
            SiteServiceRequestCommandPayload(
                command_id=command.command_id,
                command_key=command.command_key,
                ticket_id=command.ticket_id,
                reply_text=command.reply_text,
                lease_until=command.lease_until,
                lease_token=command.lease_token,
            )
            for command in commands
        ]
    )


@router.post(
    "/commands/{command_id}/ack",
    response_model=SiteServiceRequestCommandAckResponse,
    responses={
        401: {"description": "Invalid or missing site HMAC authentication"},
        404: {"description": "Command is not registered"},
        409: {"description": "Nonce replay or command acknowledgement conflict"},
        413: {"description": "Request body exceeds the configured limit"},
        503: {"description": "Command storage is unavailable"},
    },
)
def acknowledge_command(
    command_id: int,
    payload: SiteServiceRequestCommandAckPayload,
    _verified: VerifiedSiteRequest = Depends(require_site_service_request_signature),
    db: Session = Depends(get_db),
) -> SiteServiceRequestCommandAckResponse:
    try:
        result = acknowledge_site_service_request_command(
            db,
            command_id=command_id,
            payload=payload,
        )
        db.commit()
    except SiteServiceRequestNotFoundError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=exc.code) from exc
    except SiteServiceRequestConflictError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=exc.code) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="command_storage_unavailable") from exc

    return SiteServiceRequestCommandAckResponse(
        command_id=result.command_id,
        status=result.status,
        duplicate=result.duplicate,
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


def _filename_from_content_disposition(value: str | None) -> str:
    if value is None:
        raise HTTPException(status_code=422, detail="file_metadata_invalid")
    message = Message()
    message["Content-Disposition"] = value
    filename = message.get_filename()
    if not filename:
        raise HTTPException(status_code=422, detail="file_metadata_invalid")
    return filename


def _commit_site_service_request_file_failure(db: Session) -> None:
    try:
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="file_storage_unavailable") from exc
