from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
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
    SiteServiceRequestSource,
)
from app.schemas.site_service_requests import (
    SITE_SERVICE_REQUEST_REPLY_MAX_LENGTH,
    SiteServiceEmailEventPayload,
    SiteServiceRequestCommandAckPayload,
    SiteServiceRequestEventPayload,
)

_UNAVAILABLE_FILE_SHA256 = "0" * 64


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
    error_code: str | None = None


@dataclass(frozen=True)
class LeasedSiteServiceRequestCommand:
    command_id: int
    command_key: str
    ticket_id: int
    reply_text: str
    lease_until: datetime
    lease_token: str


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
                select(SiteServiceRequestCase)
                .where(SiteServiceRequestCase.source_ticket_id == payload.ticket.id)
                .with_for_update()
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
            case.updated_at = current_time

            event = SiteServiceRequestEvent(
                event_id=payload.event_id,
                case_id=case.id,
                source_message_id=payload.source_message_id,
                event_type=payload.event_type,
                direction="inbound",
                payload_encrypted=cipher.encrypt(raw_body, event_id=payload.event_id),
                payload_sha256=payload_sha256,
                source_message_sha256=_source_message_payload_sha256(payload),
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


def accept_site_service_email_event(
    session: Session,
    *,
    payload: SiteServiceEmailEventPayload,
    raw_body: bytes,
    payload_sha256: str,
    cipher: SiteServiceRequestCipher,
    now: datetime | None = None,
) -> AcceptedSiteServiceRequestEvent:
    """Persist a PII-free email event and bind its thread to one service case."""

    existing_event = session.scalar(
        select(SiteServiceRequestEvent).where(
            SiteServiceRequestEvent.event_id == payload.event_id
        )
    )
    if existing_event is not None:
        if existing_event.payload_sha256 != payload_sha256:
            raise SiteServiceRequestConflictError("event_payload_conflict")
        return AcceptedSiteServiceRequestEvent(
            event_id=existing_event.event_id,
            duplicate=True,
            missing_file_ids=[],
        )

    current_time = _as_utc(now or datetime.now(UTC))
    source_kind = "bitrix_mail"
    try:
        with session.begin_nested():
            source = session.scalar(
                select(SiteServiceRequestSource)
                .where(
                    SiteServiceRequestSource.source_kind == source_kind,
                    SiteServiceRequestSource.source_key == payload.source_key,
                )
                .with_for_update()
            )
            case = source.case if source is not None else None

            preferred_case = None
            if payload.existing_service_item_id is not None:
                preferred_case = session.scalar(
                    select(SiteServiceRequestCase)
                    .where(
                        SiteServiceRequestCase.bitrix_item_id
                        == payload.existing_service_item_id
                    )
                    .with_for_update()
                )
                if (
                    case is not None
                    and case.bitrix_item_id is not None
                    and case.bitrix_item_id != payload.existing_service_item_id
                ):
                    raise SiteServiceRequestConflictError(
                        "email_source_item_conflict"
                    )
                if case is not None and preferred_case is not None and case.id != preferred_case.id:
                    raise SiteServiceRequestConflictError(
                        "email_source_item_conflict"
                    )
                if case is None:
                    case = preferred_case

            if case is None:
                case = SiteServiceRequestCase(
                    source_ticket_id=_email_source_ticket_id(payload.source_key),
                    source_kind=source_kind,
                    source_key=payload.source_key,
                    source_mailbox=payload.mailbox,
                    source_thread_id=payload.thread_id,
                    primary_activity_id=(
                        payload.activity_id if payload.event_type == "email.received" else None
                    ),
                    bitrix_item_id=payload.existing_service_item_id,
                    first_seen_at=_as_utc(payload.occurred_at),
                )
                session.add(case)
                session.flush()
            elif _as_utc(payload.occurred_at) < _as_utc(case.first_seen_at):
                case.first_seen_at = _as_utc(payload.occurred_at)

            if source is None:
                source = SiteServiceRequestSource(
                    case_id=case.id,
                    source_kind=source_kind,
                    source_key=payload.source_key,
                    source_mailbox=payload.mailbox,
                    source_thread_id=payload.thread_id,
                    primary_activity_id=(
                        payload.activity_id if payload.event_type == "email.received" else None
                    ),
                    created_at=current_time,
                    updated_at=current_time,
                )
                session.add(source)
            elif (
                source.source_mailbox != payload.mailbox
                or source.source_thread_id != payload.thread_id
            ):
                raise SiteServiceRequestConflictError("email_source_identity_conflict")

            if payload.event_type == "email.received":
                if source.primary_activity_id is None:
                    source.primary_activity_id = payload.activity_id
                if case.source_kind == source_kind and case.primary_activity_id is None:
                    case.primary_activity_id = payload.activity_id
                case.latest_inbound_message_id = max(
                    case.latest_inbound_message_id or 0,
                    payload.message_id,
                )
            else:
                case.latest_outbound_message_id = max(
                    case.latest_outbound_message_id or 0,
                    payload.message_id,
                )

            case.sync_status = "pending"
            case.last_error_code = None
            case.updated_at = current_time
            source.updated_at = current_time
            session.add(
                SiteServiceRequestEvent(
                    event_id=payload.event_id,
                    case_id=case.id,
                    source_message_id=payload.message_id,
                    source_activity_id=payload.activity_id,
                    event_type=payload.event_type,
                    direction=(
                        "inbound" if payload.event_type == "email.received" else "outbound"
                    ),
                    payload_encrypted=cipher.encrypt(raw_body, event_id=payload.event_id),
                    payload_sha256=payload_sha256,
                    source_message_sha256=payload_sha256,
                    status="pending",
                    created_at=current_time,
                    updated_at=current_time,
                )
            )
            session.flush()
    except IntegrityError as exc:
        concurrent_event = session.scalar(
            select(SiteServiceRequestEvent).where(
                SiteServiceRequestEvent.event_id == payload.event_id
            )
        )
        if concurrent_event is None:
            raise
        if concurrent_event.payload_sha256 != payload_sha256:
            raise SiteServiceRequestConflictError("event_payload_conflict") from exc
        return AcceptedSiteServiceRequestEvent(
            event_id=concurrent_event.event_id,
            duplicate=True,
            missing_file_ids=[],
        )

    return AcceptedSiteServiceRequestEvent(
        event_id=payload.event_id,
        duplicate=False,
        missing_file_ids=[],
    )


def _email_source_ticket_id(source_key: str) -> int:
    # Site ticket IDs are positive. A deterministic negative identity preserves
    # the historic non-null/unique column without coupling email cases to it.
    value = int.from_bytes(
        hashlib.sha256(source_key.encode("utf-8")).digest()[:7],
        byteorder="big",
        signed=False,
    )
    return -(value or 1)


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
    event_identity = session.execute(
        select(
            SiteServiceRequestEvent.case_id,
            SiteServiceRequestEvent.source_message_id,
        ).where(SiteServiceRequestEvent.event_id == event_id)
    ).one_or_none()
    if event_identity is None:
        raise SiteServiceRequestNotFoundError("event_not_found")

    case_id, source_message_id = event_identity
    case = session.scalar(
        select(SiteServiceRequestCase).where(SiteServiceRequestCase.id == case_id).with_for_update()
    )
    if case is None:
        raise SiteServiceRequestNotFoundError("event_not_found")

    files = session.scalars(
        select(SiteServiceRequestFile)
        .where(
            SiteServiceRequestFile.case_id == case.id,
            SiteServiceRequestFile.source_message_id == source_message_id,
            SiteServiceRequestFile.source_file_id == file_id,
        )
        .limit(2)
        .with_for_update()
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

        unavailable_placeholder = (
            file.sha256 == _UNAVAILABLE_FILE_SHA256
            and file.status in {"pending", "failed"}
            and file.last_error_code in {None, "file_unavailable"}
        )
        if (
            file.safe_filename != normalized_filename
            or file.mime_type.strip().lower() != normalized_mime_type
            or file.byte_size != declared_size
        ) and not unavailable_placeholder:
            raise SiteServiceRequestConflictError("file_metadata_conflict")
        if file.sha256 != calculated_sha256 and not unavailable_placeholder:
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

    # Resolve the remaining case-level file error before touching the filesystem.
    # This keeps every fallible SQL operation ahead of the atomic spool write, so
    # a database read failure cannot leave an untracked payload behind.
    remaining_file_error_code = _case_file_error_code(
        session,
        case_id=file.case_id,
        exclude_file_id=file.id,
    )
    had_existing_payload = existing_path is not None and existing_path.is_file()
    target_path = _write_site_service_request_file(
        spool_dir=spool_dir,
        case_id=case.id,
        file_row_id=file.id,
        body=body,
    )
    if unavailable_placeholder:
        file.safe_filename = normalized_filename
        file.mime_type = normalized_mime_type
        file.byte_size = declared_size
        file.sha256 = calculated_sha256
    file.temporary_path = str(target_path)
    file.status = "staged"
    file.last_error_code = None
    file.bitrix_error_reported_at = None
    file.updated_at = _as_utc(now or datetime.now(UTC))
    if remaining_file_error_code is None:
        if case.sync_status == "file_sync_error":
            case.sync_status = case.base_sync_status
            case.last_error_code = case.base_error_code
    return StagedSiteServiceRequestFile(
        event_id=event_id,
        file_id=file_id,
        status="staged",
        duplicate=False,
        storage_path=str(target_path),
        cleanup_on_failure=not had_existing_payload,
    )


def cleanup_staged_site_service_request_file(result: StagedSiteServiceRequestFile) -> None:
    if not result.cleanup_on_failure or not result.storage_path:
        return
    try:
        Path(result.storage_path).unlink(missing_ok=True)
    except OSError:
        pass


def cleanup_unreferenced_site_service_request_file(
    session: Session,
    *,
    result: StagedSiteServiceRequestFile,
) -> None:
    """Remove a failed spool write while serializing against another PUT.

    The deterministic spool path belongs to the file row, so locking by the
    durable event/file identity is required. A lookup by ``temporary_path`` can
    miss a concurrent uncommitted restage and delete its newly written payload.
    """
    if not result.cleanup_on_failure or not result.storage_path:
        return

    event_identity = session.execute(
        select(
            SiteServiceRequestEvent.case_id,
            SiteServiceRequestEvent.source_message_id,
        ).where(SiteServiceRequestEvent.event_id == result.event_id)
    ).one_or_none()
    if event_identity is None:
        # Missing identity is ambiguous after a failed commit. Retain the private
        # spool file rather than risk deleting a payload owned by another PUT.
        return
    case_id, source_message_id = event_identity
    case = session.scalar(
        select(SiteServiceRequestCase).where(SiteServiceRequestCase.id == case_id).with_for_update()
    )
    if case is None:
        return
    files = session.scalars(
        select(SiteServiceRequestFile)
        .where(
            SiteServiceRequestFile.case_id == case.id,
            SiteServiceRequestFile.source_message_id == source_message_id,
            SiteServiceRequestFile.source_file_id == result.file_id,
        )
        .limit(2)
        .with_for_update()
    ).all()
    if len(files) != 1:
        return
    if files[0].temporary_path == result.storage_path:
        return
    cleanup_staged_site_service_request_file(result)


def fail_site_service_request_file(
    session: Session,
    *,
    event_id: str,
    file_id: int,
    error_code: str,
    now: datetime | None = None,
) -> StagedSiteServiceRequestFile:
    if error_code != "file_unavailable":
        raise SiteServiceRequestPayloadError("file_error_code_invalid")
    event_identity = session.execute(
        select(
            SiteServiceRequestEvent.case_id,
            SiteServiceRequestEvent.source_message_id,
        ).where(SiteServiceRequestEvent.event_id == event_id)
    ).one_or_none()
    if event_identity is None:
        raise SiteServiceRequestNotFoundError("event_not_found")
    case_id, source_message_id = event_identity
    case = session.scalar(
        select(SiteServiceRequestCase).where(SiteServiceRequestCase.id == case_id).with_for_update()
    )
    if case is None:
        raise SiteServiceRequestNotFoundError("event_not_found")
    files = session.scalars(
        select(SiteServiceRequestFile)
        .where(
            SiteServiceRequestFile.case_id == case.id,
            SiteServiceRequestFile.source_message_id == source_message_id,
            SiteServiceRequestFile.source_file_id == file_id,
        )
        .limit(2)
        .with_for_update()
    ).all()
    if not files:
        raise SiteServiceRequestNotFoundError("file_not_registered")
    if len(files) != 1:
        raise SiteServiceRequestConflictError("file_identity_ambiguous")
    file = files[0]
    if file.status in {"staged", "uploaded"}:
        return StagedSiteServiceRequestFile(
            event_id=event_id,
            file_id=file_id,
            status=file.status,
            duplicate=True,
        )
    duplicate = file.status == "failed" and file.last_error_code == error_code
    if file.sha256 != _UNAVAILABLE_FILE_SHA256:
        file.sha256 = _UNAVAILABLE_FILE_SHA256
        file.updated_at = _as_utc(now or datetime.now(UTC))
    if duplicate:
        # A repeated signed report must not erase the durable Bitrix readback
        # checkpoint and trigger another identical card update.
        case.sync_status = "file_sync_error"
        case.last_error_code = error_code
        case.updated_at = _as_utc(now or datetime.now(UTC))
    else:
        _mark_file_sync_error(file, error_code=error_code, now=now)
    session.flush()
    return StagedSiteServiceRequestFile(
        event_id=event_id,
        file_id=file_id,
        status="failed",
        duplicate=duplicate,
        error_code=error_code,
    )


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
    lease_limit = min(max(limit, 1), 20)
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
    candidate_case_ids = list(
        dict.fromkeys(
            session.scalars(
                select(SiteServiceRequestCommand.case_id)
                .where(available)
                .order_by(
                    SiteServiceRequestCommand.created_at,
                    SiteServiceRequestCommand.id,
                )
                # Read ahead so one damaged row does not hide otherwise deliverable
                # commands behind the public batch limit.
                .limit(100)
            ).all()
        )
    )
    if not candidate_case_ids:
        return []
    locked_cases = session.scalars(
        select(SiteServiceRequestCase)
        .where(SiteServiceRequestCase.id.in_(candidate_case_ids))
        .order_by(SiteServiceRequestCase.id)
        .with_for_update(skip_locked=True)
    ).all()
    locked_case_ids = [case.id for case in locked_cases]
    if not locked_case_ids:
        return []
    commands = session.scalars(
        select(SiteServiceRequestCommand)
        .where(
            available,
            SiteServiceRequestCommand.case_id.in_(locked_case_ids),
        )
        .order_by(SiteServiceRequestCommand.created_at, SiteServiceRequestCommand.id)
        .limit(100)
        .with_for_update(skip_locked=True)
    ).all()
    lease_until = current_time + timedelta(seconds=lease_seconds)
    leased: list[LeasedSiteServiceRequestCommand] = []
    invalid_text_commands: list[SiteServiceRequestCommand] = []
    crypto_error_commands: list[SiteServiceRequestCommand] = []
    first_crypto_error: SiteServiceRequestConfigurationError | None = None
    for command in commands:
        if len(leased) >= lease_limit:
            break
        try:
            reply_bytes = cipher.decrypt(
                command.reply_encrypted,
                event_id=command.command_key,
            )
            reply_text = reply_bytes.decode("utf-8")
        except SiteServiceRequestConfigurationError as exc:
            crypto_error_commands.append(command)
            if first_crypto_error is None:
                first_crypto_error = exc
            continue
        except UnicodeDecodeError:
            invalid_text_commands.append(command)
            continue
        if (
            not reply_text.strip()
            or len(reply_text) > SITE_SERVICE_REQUEST_REPLY_MAX_LENGTH
            or hashlib.sha256(reply_bytes).hexdigest() != command.reply_sha256
        ):
            invalid_text_commands.append(command)
            continue
        command.status = "leased"
        command.attempts += 1
        command.lease_until = lease_until
        command.lease_token = secrets.token_urlsafe(32)
        command.updated_at = current_time
        leased.append(
            LeasedSiteServiceRequestCommand(
                command_id=command.id,
                command_key=command.command_key,
                ticket_id=command.case.source_ticket_id,
                reply_text=reply_text,
                lease_until=lease_until,
                lease_token=command.lease_token,
            )
        )

    # A successfully authenticated ciphertext proves that the configured key is
    # valid. Only then may other invalid rows be quarantined as corruption; if
    # every row fails authentication, fail closed so a wrong global key cannot
    # terminally discard all pending replies.
    key_is_proven = bool(leased or invalid_text_commands)
    if crypto_error_commands and not key_is_proven:
        assert first_crypto_error is not None
        raise first_crypto_error
    for command in [*invalid_text_commands, *crypto_error_commands]:
        command.status = "failed"
        command.attempts += 1
        command.lease_until = None
        command.lease_token = None
        command.ack_at = current_time
        command.card_action_cleared_at = current_time
        command.last_error_code = "command_payload_invalid"
        command.updated_at = current_time
        latest_command_id = session.scalar(
            select(SiteServiceRequestCommand.id)
            .where(SiteServiceRequestCommand.case_id == command.case_id)
            .order_by(
                SiteServiceRequestCommand.created_at.desc(),
                SiteServiceRequestCommand.id.desc(),
            )
            .limit(1)
        )
        if latest_command_id == command.id:
            existing_checkpoint = command.case.outbound_checked_at
            if existing_checkpoint is None or _as_utc(existing_checkpoint) <= current_time:
                command.case.outbound_checked_at = current_time
            command.case.outbound_last_error_code = "command_payload_invalid"
    session.flush()
    return leased


def acknowledge_site_service_request_command(
    session: Session,
    *,
    command_id: int,
    payload: SiteServiceRequestCommandAckPayload,
    now: datetime | None = None,
) -> AcknowledgedSiteServiceRequestCommand:
    command_case_id = session.scalar(
        select(SiteServiceRequestCommand.case_id).where(SiteServiceRequestCommand.id == command_id)
    )
    if command_case_id is None:
        raise SiteServiceRequestNotFoundError("command_not_found")
    # Keep the case -> command row-lock order aligned with outbound reconciliation.
    case = session.scalar(
        select(SiteServiceRequestCase)
        .where(SiteServiceRequestCase.id == command_case_id)
        .with_for_update()
    )
    if case is None:
        raise SiteServiceRequestNotFoundError("command_not_found")
    command = session.scalar(
        select(SiteServiceRequestCommand)
        .where(
            SiteServiceRequestCommand.id == command_id,
            SiteServiceRequestCommand.case_id == case.id,
        )
        .with_for_update()
    )
    if command is None:
        raise SiteServiceRequestNotFoundError("command_not_found")
    if command.status == "pending":
        raise SiteServiceRequestConflictError("command_not_leased")
    if not command.lease_token or payload.lease_token != command.lease_token:
        raise SiteServiceRequestConflictError("command_lease_conflict")

    latest_command_id = session.scalar(
        select(SiteServiceRequestCommand.id)
        .where(SiteServiceRequestCommand.case_id == case.id)
        .order_by(
            SiteServiceRequestCommand.created_at.desc(),
            SiteServiceRequestCommand.id.desc(),
        )
        .limit(1)
    )
    is_latest_command = latest_command_id == command.id

    current_time = _as_utc(now or datetime.now(UTC))
    if payload.status == "applied":
        assert payload.ticket_id is not None
        assert payload.message_id is not None
        assert payload.applied_at is not None
        if payload.ticket_id != case.source_ticket_id:
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
        case.latest_outbound_message_id = max(
            case.latest_outbound_message_id or 0,
            payload.message_id,
        )
        if is_latest_command:
            _update_site_service_request_outbound_checkpoint(
                case,
                current_time=current_time,
                error_code=None,
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
        if is_latest_command:
            _update_site_service_request_outbound_checkpoint(
                case,
                current_time=current_time,
                error_code=payload.error_code,
            )
        result_status = "failed"

    command.updated_at = current_time
    session.flush()
    return AcknowledgedSiteServiceRequestCommand(
        command_id=command.id,
        status=result_status,
        duplicate=False,
    )


def _update_site_service_request_outbound_checkpoint(
    case: SiteServiceRequestCase,
    *,
    current_time: datetime,
    error_code: str | None,
) -> bool:
    if case.outbound_checked_at is not None and _as_utc(case.outbound_checked_at) > current_time:
        return False
    case.outbound_checked_at = current_time
    case.outbound_last_error_code = error_code
    return True


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
    elif not (
        settings.site_service_requests_ingest_enabled
        or settings.site_service_requests_email_ingest_enabled
    ):
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
        "email_ingest_enabled": settings.site_service_requests_email_ingest_enabled,
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
    same_source_message = (
        event.source_message_sha256 is not None
        and event.source_message_sha256 == _source_message_payload_sha256(payload)
    )
    if event.payload_sha256 != payload_sha256 and not same_source_message:
        raise SiteServiceRequestConflictError("event_payload_conflict")
    return AcceptedSiteServiceRequestEvent(
        event_id=event.event_id,
        duplicate=True,
        missing_file_ids=_missing_file_ids(
            session,
            case_id=event.case_id,
            source_message_id=event.source_message_id,
        ),
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
                unavailable_placeholder = (
                    existing.sha256 == _UNAVAILABLE_FILE_SHA256
                    and existing.status in {"pending", "failed"}
                    and existing.last_error_code in {None, "file_unavailable"}
                )
                metadata_changed = (
                    existing.safe_filename != file.name
                    or existing.mime_type != file.mime_type
                    or existing.byte_size != file.size
                    or existing.sha256 != file.sha256
                )
                if metadata_changed and not unavailable_placeholder:
                    raise SiteServiceRequestConflictError("file_metadata_conflict")
                if unavailable_placeholder:
                    # A later event may contain restored metadata for a file from
                    # an older history message, but its upload endpoint is scoped
                    # to the later event's own source message. Keep the explicit
                    # zero placeholder until the original event performs the
                    # full binary PUT, where metadata and content are validated
                    # and replaced atomically.
                    if message.message_id == payload.source_message_id:
                        if is_oversized:
                            existing.status = "failed"
                            existing.last_error_code = "file_too_large"
                            existing.bitrix_error_reported_at = None
                            existing.updated_at = current_time
                        elif existing.status == "pending" or (
                            existing.status == "failed"
                            and existing.last_error_code == "file_unavailable"
                        ):
                            missing_file_ids.add(file.file_id)
                    continue
                if is_oversized and existing.status != "uploaded":
                    existing.status = "failed"
                    existing.last_error_code = "file_too_large"
                    existing.bitrix_error_reported_at = None
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
    *,
    case_id: int,
    source_message_id: int,
) -> list[int]:
    rows = session.scalars(
        select(SiteServiceRequestFile).where(
            SiteServiceRequestFile.case_id == case_id,
            SiteServiceRequestFile.source_message_id == source_message_id,
            or_(
                SiteServiceRequestFile.status == "pending",
                and_(
                    SiteServiceRequestFile.status == "failed",
                    SiteServiceRequestFile.last_error_code == "file_unavailable",
                    SiteServiceRequestFile.sha256 == _UNAVAILABLE_FILE_SHA256,
                ),
            ),
        )
    ).all()
    return sorted({row.source_file_id for row in rows})


def _source_message_payload_sha256(payload: SiteServiceRequestEventPayload) -> str:
    source_message = next(
        message for message in payload.history if message.message_id == payload.source_message_id
    )
    canonical_message = source_message.model_dump(mode="json", by_alias=True)
    # A deleted/unavailable b_file row has only its durable attachment ID. Name,
    # MIME, size and hash may all reappear between retries, so only file identity
    # participates in semantic event dedupe; the upload endpoint validates the
    # recovered metadata and content against the explicit zero placeholder.
    canonical_message["files"] = [
        {"fileId": file_id}
        for file_id in sorted(
            file_payload.get("fileId")
            for file_payload in canonical_message.get("files", [])
            if isinstance(file_payload, dict)
        )
    ]
    canonical = json.dumps(
        canonical_message,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _mark_file_sync_error(
    file: SiteServiceRequestFile,
    *,
    error_code: str,
    now: datetime | None,
) -> None:
    current_time = _as_utc(now or datetime.now(UTC))
    file.status = "failed"
    file.last_error_code = error_code
    file.bitrix_error_reported_at = None
    file.updated_at = current_time
    file.case.sync_status = "file_sync_error"
    file.case.last_error_code = error_code
    file.case.updated_at = current_time


def _case_file_error_code(
    session: Session,
    *,
    case_id: int,
    exclude_file_id: int | None = None,
) -> str | None:
    predicates = [
        SiteServiceRequestFile.case_id == case_id,
        SiteServiceRequestFile.status == "failed",
        SiteServiceRequestFile.last_error_code.is_not(None),
    ]
    if exclude_file_id is not None:
        predicates.append(SiteServiceRequestFile.id != exclude_file_id)
    return session.scalar(
        select(SiteServiceRequestFile.last_error_code)
        .where(*predicates)
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
        # The temporary file already has mode 0600; os.replace preserves it.
        # Avoid a second fallible filesystem operation after the atomic replace.
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
