from __future__ import annotations

import base64
import hashlib
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.site_service_requests import (
    SiteServiceRequestCase,
    SiteServiceRequestCommand,
    SiteServiceRequestEvent,
    SiteServiceRequestFile,
)
from app.schemas.site_service_requests import (
    SiteServiceRequestCommandAckPayload,
    SiteServiceRequestEventPayload,
)


class SiteServiceRequestConfigurationError(RuntimeError):
    pass


class SiteServiceRequestConflictError(RuntimeError):
    def __init__(self, code: str, *, persist_state: bool = False):
        super().__init__(code)
        self.code = code
        self.persist_state = persist_state


class SiteServiceRequestNotFoundError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class SiteServiceRequestPayloadError(RuntimeError):
    def __init__(self, code: str, *, persist_state: bool = False):
        super().__init__(code)
        self.code = code
        self.persist_state = persist_state


class SiteServiceRequestStorageError(RuntimeError):
    pass


@dataclass(frozen=True)
class AcceptedSiteServiceRequestEvent:
    event_id: str
    duplicate: bool
    missing_file_ids: list[int]


@dataclass(frozen=True)
class StagedSiteServiceRequestFile:
    event_id: str
    file_id: int
    status: str
    duplicate: bool
    storage_path: str | None = None
    cleanup_on_failure: bool = False


@dataclass(frozen=True)
class LeasedSiteServiceRequestCommand:
    command_id: int
    command_key: str
    ticket_id: int
    reply_text: str
    lease_until: datetime


@dataclass(frozen=True)
class AcknowledgedSiteServiceRequestCommand:
    command_id: int
    status: str
    duplicate: bool


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
            elif _as_utc(payload.occurred_at) < _as_utc(case.first_seen_at):
                case.first_seen_at = _as_utc(payload.occurred_at)

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
            case.base_sync_status = "pending"
            case.base_error_code = None
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
            file_error_code = _case_file_error_code(session, case_id=case.id)
            if file_error_code:
                case.sync_status = "file_sync_error"
                case.last_error_code = file_error_code
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


def stage_site_service_request_file(
    session: Session,
    *,
    event_id: str,
    file_id: int,
    body: bytes,
    safe_filename: str,
    mime_type: str,
    declared_size: int,
    body_sha256: str,
    spool_dir: str,
    max_file_bytes: int,
    now: datetime | None = None,
) -> StagedSiteServiceRequestFile:
    event = session.scalar(
        select(SiteServiceRequestEvent).where(SiteServiceRequestEvent.event_id == event_id)
    )
    if event is None:
        raise SiteServiceRequestNotFoundError("event_not_found")

    files = session.scalars(
        select(SiteServiceRequestFile)
        .where(
            SiteServiceRequestFile.case_id == event.case_id,
            SiteServiceRequestFile.source_message_id == event.source_message_id,
            SiteServiceRequestFile.source_file_id == file_id,
        )
        .limit(2)
    ).all()
    if not files:
        raise SiteServiceRequestNotFoundError("file_not_registered")
    if len(files) != 1:
        raise SiteServiceRequestConflictError("file_identity_ambiguous")
    file = files[0]

    # Once Bitrix has confirmed this file, a malformed transport retry must not
    # regress the durable upload state.
    if file.status == "uploaded":
        return StagedSiteServiceRequestFile(
            event_id=event_id,
            file_id=file_id,
            status="uploaded",
            duplicate=True,
        )

    try:
        normalized_filename = _normalize_safe_filename(safe_filename)
        normalized_mime_type = mime_type.split(";", 1)[0].strip().lower()
        if not normalized_mime_type:
            raise SiteServiceRequestPayloadError("file_mime_type_invalid")
        if declared_size < 0 or declared_size != len(body):
            raise SiteServiceRequestPayloadError("file_size_mismatch")
        if declared_size > max_file_bytes:
            raise SiteServiceRequestPayloadError("file_too_large")

        calculated_sha256 = hashlib.sha256(body).hexdigest()
        if calculated_sha256 != body_sha256:
            raise SiteServiceRequestPayloadError("file_hash_mismatch")

        if (
            file.safe_filename != normalized_filename
            or file.mime_type.strip().lower() != normalized_mime_type
            or file.byte_size != declared_size
        ):
            raise SiteServiceRequestConflictError("file_metadata_conflict")
        if file.sha256 != calculated_sha256:
            raise SiteServiceRequestPayloadError("file_hash_mismatch")
    except (SiteServiceRequestPayloadError, SiteServiceRequestConflictError) as exc:
        _mark_file_sync_error(file, error_code=exc.code, now=now)
        session.flush()
        exc.persist_state = True
        raise

    existing_path = Path(file.temporary_path) if file.temporary_path else None
    if file.status == "staged" and existing_path is not None and existing_path.is_file():
        try:
            existing_sha256 = hashlib.sha256(existing_path.read_bytes()).hexdigest()
        except OSError as exc:
            raise SiteServiceRequestStorageError("file_storage_unavailable") from exc
        if existing_sha256 == calculated_sha256:
            return StagedSiteServiceRequestFile(
                event_id=event_id,
                file_id=file_id,
                status="staged",
                duplicate=True,
                storage_path=str(existing_path),
            )

    target_path = _write_site_service_request_file(
        spool_dir=spool_dir,
        case_id=event.case_id,
        file_row_id=file.id,
        body=body,
    )
    file.temporary_path = str(target_path)
    file.status = "staged"
    file.last_error_code = None
    file.updated_at = _as_utc(now or datetime.now(UTC))
    if _case_file_error_code(session, case_id=file.case_id) is None:
        if file.case.sync_status == "file_sync_error":
            file.case.sync_status = file.case.base_sync_status
            file.case.last_error_code = file.case.base_error_code
    return StagedSiteServiceRequestFile(
        event_id=event_id,
        file_id=file_id,
        status="staged",
        duplicate=False,
        storage_path=str(target_path),
        cleanup_on_failure=True,
    )


def cleanup_staged_site_service_request_file(result: StagedSiteServiceRequestFile) -> None:
    if not result.cleanup_on_failure or not result.storage_path:
        return
    try:
        Path(result.storage_path).unlink(missing_ok=True)
    except OSError:
        pass


def lease_site_service_request_commands(
    session: Session,
    *,
    cipher: SiteServiceRequestCipher,
    enabled: bool,
    lease_seconds: int,
    limit: int = 20,
    now: datetime | None = None,
) -> list[LeasedSiteServiceRequestCommand]:
    if not enabled:
        return []
    current_time = _as_utc(now or datetime.now(UTC))
    available = or_(
        SiteServiceRequestCommand.status == "pending",
        and_(
            SiteServiceRequestCommand.status == "leased",
            or_(
                SiteServiceRequestCommand.lease_until.is_(None),
                SiteServiceRequestCommand.lease_until <= current_time,
            ),
        ),
    )
    commands = session.scalars(
        select(SiteServiceRequestCommand)
        .where(available)
        .order_by(SiteServiceRequestCommand.created_at, SiteServiceRequestCommand.id)
        .limit(min(max(limit, 1), 20))
        .with_for_update(skip_locked=True)
    ).all()
    lease_until = current_time + timedelta(seconds=lease_seconds)
    leased: list[LeasedSiteServiceRequestCommand] = []
    for command in commands:
        try:
            reply_text = cipher.decrypt(
                command.reply_encrypted,
                event_id=command.command_key,
            ).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SiteServiceRequestConfigurationError(
                "site service request command text is invalid"
            ) from exc
        if not reply_text:
            raise SiteServiceRequestConfigurationError(
                "site service request command text is invalid"
            )
        command.status = "leased"
        command.attempts += 1
        command.lease_until = lease_until
        command.updated_at = current_time
        leased.append(
            LeasedSiteServiceRequestCommand(
                command_id=command.id,
                command_key=command.command_key,
                ticket_id=command.case.source_ticket_id,
                reply_text=reply_text,
                lease_until=lease_until,
            )
        )
    session.flush()
    return leased


def acknowledge_site_service_request_command(
    session: Session,
    *,
    command_id: int,
    payload: SiteServiceRequestCommandAckPayload,
    now: datetime | None = None,
) -> AcknowledgedSiteServiceRequestCommand:
    command = session.scalar(
        select(SiteServiceRequestCommand)
        .where(SiteServiceRequestCommand.id == command_id)
        .with_for_update()
    )
    if command is None:
        raise SiteServiceRequestNotFoundError("command_not_found")
    if command.status == "pending":
        raise SiteServiceRequestConflictError("command_not_leased")

    current_time = _as_utc(now or datetime.now(UTC))
    if payload.status == "applied":
        assert payload.ticket_id is not None
        assert payload.message_id is not None
        assert payload.applied_at is not None
        if payload.ticket_id != command.case.source_ticket_id:
            raise SiteServiceRequestConflictError("command_ticket_conflict")
        if command.status == "failed":
            raise SiteServiceRequestConflictError("command_ack_conflict")
        if command.status == "applied":
            if command.source_message_id != payload.message_id:
                raise SiteServiceRequestConflictError("command_ack_conflict")
            return AcknowledgedSiteServiceRequestCommand(
                command_id=command.id,
                status="applied",
                duplicate=True,
            )

        command.status = "applied"
        command.source_message_id = payload.message_id
        command.ack_at = _as_utc(payload.applied_at)
        command.last_error_code = None
        command.lease_until = None
        command.case.latest_outbound_message_id = max(
            command.case.latest_outbound_message_id or 0,
            payload.message_id,
        )
        result_status = "applied"
    else:
        assert payload.error_code is not None
        if command.status == "applied":
            raise SiteServiceRequestConflictError("command_ack_conflict")
        if command.status == "failed":
            if command.last_error_code != payload.error_code:
                raise SiteServiceRequestConflictError("command_ack_conflict")
            return AcknowledgedSiteServiceRequestCommand(
                command_id=command.id,
                status="failed",
                duplicate=True,
            )
        command.status = "failed"
        command.ack_at = current_time
        command.last_error_code = payload.error_code
        command.lease_until = None
        result_status = "failed"

    command.updated_at = current_time
    session.flush()
    return AcknowledgedSiteServiceRequestCommand(
        command_id=command.id,
        status=result_status,
        duplicate=False,
    )


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
    assignment_failures = _count(
        session,
        SiteServiceRequestCase,
        SiteServiceRequestCase.assignment_last_error_code.is_not(None),
    )
    outbound_failures = _count(
        session,
        SiteServiceRequestCase,
        SiteServiceRequestCase.outbound_last_error_code.is_not(None),
    )
    pending_escalation_predicates = [
        SiteServiceRequestCase.escalation_timeline_delivered_at.is_(None)
    ]
    if settings.site_service_requests_escalation_user_id is not None:
        pending_escalation_predicates.append(
            SiteServiceRequestCase.escalation_notification_delivered_at.is_(None)
        )
    pending_escalation_deliveries = _count(
        session,
        SiteServiceRequestCase,
        and_(
            SiteServiceRequestCase.escalated_at.is_not(None),
            or_(*pending_escalation_predicates),
        ),
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

    alert_codes: list[str] = []
    if failed_events:
        alert_codes.append("dead_letter")
    if (
        oldest_pending_lag_seconds is not None
        and oldest_pending_lag_seconds >= settings.site_service_requests_health_lag_alert_seconds
    ):
        alert_codes.append("event_lag")
    if assignment_failures:
        alert_codes.append("assignment_failure")
    if outbound_failures:
        alert_codes.append("outbound_failure")
    if pending_escalation_deliveries:
        alert_codes.append("escalation_delivery_pending")

    if alert_codes:
        status = "degraded"
    elif not settings.site_service_requests_ingest_enabled:
        status = "disabled"
    else:
        status = "healthy"

    return {
        "status": status,
        "alert_codes": alert_codes,
        "pending_events": pending_events,
        "failed_events": failed_events,
        "oldest_pending_lag_seconds": oldest_pending_lag_seconds,
        "pending_commands": pending_commands,
        "unlinked_cases": unlinked_cases,
        "assignment_failures": assignment_failures,
        "outbound_failures": outbound_failures,
        "pending_escalation_deliveries": pending_escalation_deliveries,
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
            is_oversized = file.size > max_file_bytes
            existing = session.scalar(
                select(SiteServiceRequestFile).where(
                    SiteServiceRequestFile.source_message_id == message.message_id,
                    SiteServiceRequestFile.source_file_id == file.file_id,
                )
            )
            if existing is not None:
                if existing.sha256 != file.sha256 or existing.byte_size != file.size:
                    raise SiteServiceRequestConflictError("file_metadata_conflict")
                if is_oversized and existing.status != "uploaded":
                    existing.status = "failed"
                    existing.last_error_code = "file_too_large"
                    existing.updated_at = current_time
                elif (
                    message.message_id == payload.source_message_id and existing.status == "pending"
                ):
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
                    status="failed" if is_oversized else "pending",
                    last_error_code="file_too_large" if is_oversized else None,
                    created_at=current_time,
                    updated_at=current_time,
                )
            )
            if not is_oversized and message.message_id == payload.source_message_id:
                missing_file_ids.add(file.file_id)
    return sorted(missing_file_ids)


def _missing_file_ids(
    session: Session,
    case_id: int,
    payload: SiteServiceRequestEventPayload,
) -> list[int]:
    requested = {
        (message.message_id, file.file_id)
        for message in payload.history
        if message.message_id == payload.source_message_id
        for file in message.files
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
            if (row.source_message_id, row.source_file_id) in requested and row.status == "pending"
        }
    )


def _mark_file_sync_error(
    file: SiteServiceRequestFile,
    *,
    error_code: str,
    now: datetime | None,
) -> None:
    current_time = _as_utc(now or datetime.now(UTC))
    file.status = "failed"
    file.last_error_code = error_code
    file.updated_at = current_time
    file.case.sync_status = "file_sync_error"
    file.case.last_error_code = error_code
    file.case.updated_at = current_time


def _case_file_error_code(session: Session, *, case_id: int) -> str | None:
    return session.scalar(
        select(SiteServiceRequestFile.last_error_code)
        .where(
            SiteServiceRequestFile.case_id == case_id,
            SiteServiceRequestFile.status == "failed",
            SiteServiceRequestFile.last_error_code.is_not(None),
        )
        .order_by(SiteServiceRequestFile.updated_at.desc(), SiteServiceRequestFile.id.desc())
        .limit(1)
    )


def _normalize_safe_filename(value: str) -> str:
    normalized = value.strip()
    if (
        normalized in {"", ".", ".."}
        or "/" in normalized
        or "\\" in normalized
        or "\x00" in normalized
    ):
        raise SiteServiceRequestPayloadError("file_name_invalid")
    if len(normalized) > 255:
        raise SiteServiceRequestPayloadError("file_name_invalid")
    return normalized


def _write_site_service_request_file(
    *,
    spool_dir: str,
    case_id: int,
    file_row_id: int,
    body: bytes,
) -> Path:
    temporary_path: str | None = None
    try:
        root = Path(spool_dir).expanduser().resolve()
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        case_dir = root / str(case_id)
        case_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(case_dir, 0o700)
        file_descriptor, temporary_path = tempfile.mkstemp(
            dir=case_dir,
            prefix=f".{file_row_id}-",
            suffix=".part",
        )
        with os.fdopen(file_descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        target_path = case_dir / f"{file_row_id}.bin"
        os.replace(temporary_path, target_path)
        temporary_path = None
        os.chmod(target_path, 0o600)
        return target_path
    except OSError as exc:
        if temporary_path:
            try:
                Path(temporary_path).unlink(missing_ok=True)
            except OSError:
                pass
        raise SiteServiceRequestStorageError("file_storage_unavailable") from exc


def _count(session: Session, model, predicate) -> int:
    return int(session.scalar(select(func.count()).select_from(model).where(predicate)) or 0)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
