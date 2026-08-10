"""Authenticated internal API for the encrypted SMS journal."""

from __future__ import annotations

from typing import Annotated, Any, Callable
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.dependencies import get_db, require_sms_journal_internal_token
from app.core.config import get_settings
from app.schemas.sms_journal import (
    SmsAttemptCreateRequest,
    SmsAttemptResponse,
    SmsDeliveryUpdateRequest,
    SmsSendResultRequest,
)
from app.services.sms_journal import (
    SmsJournalCipher,
    SmsJournalConfigurationError,
    SmsJournalConflictError,
    SmsJournalNotFoundError,
    SmsJournalService,
)

router = APIRouter(
    prefix="/api/internal/sms-journal",
    tags=["sms-journal"],
    dependencies=[Depends(require_sms_journal_internal_token)],
)

IdempotencyKey = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=8, max_length=255),
]


def _service(db: Session) -> SmsJournalService:
    settings = get_settings()
    if not settings.sms_journal_encryption_key or not settings.sms_journal_phone_hash_key:
        raise SmsJournalConfigurationError("SMS journal encryption is not configured")
    return SmsJournalService(
        db,
        SmsJournalCipher(
            settings.sms_journal_encryption_key,
            settings.sms_journal_phone_hash_key,
        ),
    )


def _write(db: Session, command: Callable[[], Any]) -> Any:
    try:
        response = command()
        db.commit()
        return response
    except SmsJournalNotFoundError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SmsJournalConflictError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="concurrent SMS journal conflict") from exc
    except (SmsJournalConfigurationError, SQLAlchemyError) as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="SMS journal unavailable") from exc


@router.post("/attempts", response_model=SmsAttemptResponse, status_code=201)
def create_attempt(
    payload: SmsAttemptCreateRequest,
    idempotency_key: IdempotencyKey,
    db: Session = Depends(get_db),
) -> SmsAttemptResponse:
    response = _write(
        db,
        lambda: _service(db).create_attempt(
            idempotency_key=idempotency_key,
            payload=payload.model_dump(),
        ),
    )
    return SmsAttemptResponse.model_validate(response)


@router.post("/attempts/{event_id}/send-result", response_model=SmsAttemptResponse)
def record_send_result(
    event_id: UUID,
    payload: SmsSendResultRequest,
    idempotency_key: IdempotencyKey,
    db: Session = Depends(get_db),
) -> SmsAttemptResponse:
    response = _write(
        db,
        lambda: _service(db).record_send_result(
            event_id,
            idempotency_key=idempotency_key,
            payload=payload.model_dump(),
        ),
    )
    return SmsAttemptResponse.model_validate(response)


@router.post("/attempts/{event_id}/delivery", response_model=SmsAttemptResponse)
def update_delivery(
    event_id: UUID,
    payload: SmsDeliveryUpdateRequest,
    idempotency_key: IdempotencyKey,
    db: Session = Depends(get_db),
) -> SmsAttemptResponse:
    response = _write(
        db,
        lambda: _service(db).update_delivery(
            event_id,
            idempotency_key=idempotency_key,
            payload=payload.model_dump(),
        ),
    )
    return SmsAttemptResponse.model_validate(response)


@router.get("/attempts/{event_id}", response_model=SmsAttemptResponse)
def get_attempt(event_id: UUID, db: Session = Depends(get_db)) -> SmsAttemptResponse:
    try:
        return SmsAttemptResponse.model_validate(_service(db).get_attempt(event_id))
    except SmsJournalNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (SmsJournalConfigurationError, SQLAlchemyError) as exc:
        raise HTTPException(status_code=503, detail="SMS journal unavailable") from exc
