from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.site_service_requests import (
    SiteServiceRequestCase,
    SiteServiceRequestCommand,
    SiteServiceRequestEvent,
    SiteServiceRequestFile,
)
from app.schemas.site_service_requests import SiteServiceRequestEventPayload


class SiteServiceRequestConfigurationError(RuntimeError):
    pass


class SiteServiceRequestConflictError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class AcceptedSiteServiceRequestEvent:
    event_id: str
    duplicate: bool
    missing_file_ids: list[int]


class SiteServiceRequestCipher:
    def __init__(self, encryption_key: str) -> None:
        try:
            key = base64.urlsafe_b64decode(encryption_key.encode("ascii"))
        except (ValueError, UnicodeError) as exc:
            raise SiteServiceRequestConfigurationError(
                "site service request encryption key is invalid"
            ) from exc
        if len(key) != 32:
            raise SiteServiceRequestConfigurationError(
                "site service request encryption key must decode to 32 bytes"
            )
        self._cipher = AESGCM(key)

    def encrypt(self, value: bytes, *, event_id: str) -> bytes:
        nonce = os.urandom(12)
        ciphertext = self._cipher.encrypt(nonce, value, event_id.encode("utf-8"))
        return nonce + ciphertext

    def decrypt(self, value: bytes, *, event_id: str) -> bytes:
        try:
            if len(value) <= 12:
                raise ValueError("ciphertext is too short")
            return self._cipher.decrypt(
                value[:12],
                value[12:],
                event_id.encode("utf-8"),
            )
        except (InvalidTag, ValueError) as exc:
            raise SiteServiceRequestConfigurationError(
                "site service request ciphertext is invalid"
            ) from exc


def build_site_service_request_cipher(settings: Settings) -> SiteServiceRequestCipher:
    key = str(settings.site_service_requests_event_encryption_key or "")
    if not key:
        raise SiteServiceRequestConfigurationError(
            "site service request encryption is not configured"
        )
    return SiteServiceRequestCipher(key)


def accept_site_service_request_event(
    session: Session,
    *,
    payload: SiteServiceRequestEventPayload,
    raw_body: bytes,
    payload_sha256: str,
    cipher: SiteServiceRequestCipher,
    max_file_bytes: int,
    now: datetime | None = None,
) -> AcceptedSiteServiceRequestEvent:
    validate_site_service_request_files(payload, max_file_bytes=max_file_bytes)
    existing_event = session.scalar(
        select(SiteServiceRequestEvent).where(SiteServiceRequestEvent.event_id == payload.event_id)
    )
    if existing_event is not None:
        return _existing_event_result(
            session,
            event=existing_event,
            payload=payload,
            payload_sha256=payload_sha256,
        )

    current_time = _as_utc(now or datetime.now(UTC))
    try:
        with session.begin_nested():
            case = session.scalar(
                select(SiteServiceRequestCase).where(
                    SiteServiceRequestCase.source_ticket_id == payload.ticket.id
                )
            )
            if case is None:
                case = SiteServiceRequestCase(
                    source_ticket_id=payload.ticket.id,
                    first_seen_at=_as_utc(payload.occurred_at),
                )
                session.add(case)
                session.flush()

            latest_customer_message = max(
                (
                    message.message_id
                    for message in payload.history
                    if message.author_kind == "customer"
                ),
                default=None,
            )
            if latest_customer_message is not None:
                case.latest_inbound_message_id = max(
                    case.latest_inbound_message_id or 0,
                    latest_customer_message,
                )
            case.sync_status = "pending"
            case.last_error_code = None
            case.updated_at = current_time

            event = SiteServiceRequestEvent(
                event_id=payload.event_id,
                case_id=case.id,
                source_message_id=payload.source_message_id,
                event_type=payload.event_type,
                direction="inbound",
                payload_encrypted=cipher.encrypt(raw_body, event_id=payload.event_id),
                payload_sha256=payload_sha256,
                status="pending",
                created_at=current_time,
                updated_at=current_time,
            )
            session.add(event)
            missing_file_ids = _upsert_file_metadata(
                session,
                case=case,
                payload=payload,
                max_file_bytes=max_file_bytes,
                current_time=current_time,
            )
            session.flush()
    except IntegrityError:
        concurrent_event = session.scalar(
            select(SiteServiceRequestEvent).where(
                SiteServiceRequestEvent.event_id == payload.event_id
            )
        )
        if concurrent_event is None:
            raise
        return _existing_event_result(
            session,
            event=concurrent_event,
            payload=payload,
            payload_sha256=payload_sha256,
        )

    return AcceptedSiteServiceRequestEvent(
        event_id=payload.event_id,
        duplicate=False,
        missing_file_ids=missing_file_ids,
    )


def validate_site_service_request_files(
    payload: SiteServiceRequestEventPayload,
    *,
    max_file_bytes: int,
) -> None:
    for message in payload.history:
        for file in message.files:
            if file.size > max_file_bytes:
                raise SiteServiceRequestConflictError("file_too_large")


def build_site_service_request_health(
    session: Session,
    *,
    settings: Settings,
    now: datetime | None = None,
) -> dict[str, Any]:
    pending_statuses = ("pending", "retry", "processing")
    failed_statuses = ("failed", "needs_attention")
    pending_events = _count(
        session,
        SiteServiceRequestEvent,
        SiteServiceRequestEvent.status.in_(pending_statuses),
    )
    failed_events = _count(
        session,
        SiteServiceRequestEvent,
        SiteServiceRequestEvent.status.in_(failed_statuses),
    )
    oldest_pending_at = session.scalar(
        select(func.min(SiteServiceRequestEvent.created_at)).where(
            SiteServiceRequestEvent.status.in_(pending_statuses)
        )
    )
    pending_commands = _count(
        session,
        SiteServiceRequestCommand,
        SiteServiceRequestCommand.status.in_(("pending", "leased")),
    )
    unlinked_cases = _count(
        session,
        SiteServiceRequestCase,
        SiteServiceRequestCase.bitrix_item_id.is_(None),
    )
    last_successful_exchange_at = session.scalar(
        select(func.max(SiteServiceRequestEvent.processed_at)).where(
            SiteServiceRequestEvent.status == "processed"
        )
    )

    current_time = _as_utc(now or datetime.now(UTC))
    oldest_pending_lag_seconds = None
    if oldest_pending_at is not None:
        lag = current_time - _as_utc(oldest_pending_at)
        oldest_pending_lag_seconds = max(0, int(lag.total_seconds()))

    if not settings.site_service_requests_ingest_enabled:
        status = "disabled"
    elif failed_events:
        status = "degraded"
    else:
        status = "healthy"

    return {
        "status": status,
        "pending_events": pending_events,
        "failed_events": failed_events,
        "oldest_pending_lag_seconds": oldest_pending_lag_seconds,
        "pending_commands": pending_commands,
        "unlinked_cases": unlinked_cases,
        "last_successful_exchange_at": (
            _as_utc(last_successful_exchange_at)
            if last_successful_exchange_at is not None
            else None
        ),
        "ingest_enabled": settings.site_service_requests_ingest_enabled,
        "bitrix_writes_enabled": settings.site_service_requests_bitrix_writes_enabled,
        "outbound_replies_enabled": settings.site_service_requests_outbound_replies_enabled,
    }


def _existing_event_result(
    session: Session,
    *,
    event: SiteServiceRequestEvent,
    payload: SiteServiceRequestEventPayload,
    payload_sha256: str,
) -> AcceptedSiteServiceRequestEvent:
    if event.payload_sha256 != payload_sha256:
        raise SiteServiceRequestConflictError("event_payload_conflict")
    return AcceptedSiteServiceRequestEvent(
        event_id=event.event_id,
        duplicate=True,
        missing_file_ids=_missing_file_ids(session, event.case_id, payload),
    )


def _upsert_file_metadata(
    session: Session,
    *,
    case: SiteServiceRequestCase,
    payload: SiteServiceRequestEventPayload,
    max_file_bytes: int,
    current_time: datetime,
) -> list[int]:
    missing_file_ids: set[int] = set()
    for message in payload.history:
        for file in message.files:
            if file.size > max_file_bytes:
                raise SiteServiceRequestConflictError("file_too_large")
            existing = session.scalar(
                select(SiteServiceRequestFile).where(
                    SiteServiceRequestFile.source_message_id == message.message_id,
                    SiteServiceRequestFile.source_file_id == file.file_id,
                )
            )
            if existing is not None:
                if existing.sha256 != file.sha256 or existing.byte_size != file.size:
                    raise SiteServiceRequestConflictError("file_metadata_conflict")
                if existing.status != "uploaded":
                    missing_file_ids.add(file.file_id)
                continue
            session.add(
                SiteServiceRequestFile(
                    case_id=case.id,
                    source_message_id=message.message_id,
                    source_file_id=file.file_id,
                    safe_filename=file.name,
                    mime_type=file.mime_type,
                    byte_size=file.size,
                    sha256=file.sha256,
                    status="pending",
                    created_at=current_time,
                    updated_at=current_time,
                )
            )
            missing_file_ids.add(file.file_id)
    return sorted(missing_file_ids)


def _missing_file_ids(
    session: Session,
    case_id: int,
    payload: SiteServiceRequestEventPayload,
) -> list[int]:
    requested = {
        (message.message_id, file.file_id) for message in payload.history for file in message.files
    }
    if not requested:
        return []
    rows = session.scalars(
        select(SiteServiceRequestFile).where(SiteServiceRequestFile.case_id == case_id)
    ).all()
    return sorted(
        {
            row.source_file_id
            for row in rows
            if (row.source_message_id, row.source_file_id) in requested and row.status != "uploaded"
        }
    )


def _count(session: Session, model, predicate) -> int:
    return int(session.scalar(select(func.count()).select_from(model).where(predicate)) or 0)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
