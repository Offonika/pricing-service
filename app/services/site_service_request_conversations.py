from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.site_service_requests import (
    SiteServiceRequestCase,
    SiteServiceRequestCommand,
    SiteServiceRequestCommandFile,
    SiteServiceRequestFile,
    SiteServiceRequestMessage,
)
from app.schemas.site_service_requests import SITE_SERVICE_REQUEST_REPLY_MAX_LENGTH
from app.services.site_service_requests import (
    SiteServiceRequestCipher,
    SiteServiceRequestConfigurationError,
    SiteServiceRequestConflictError,
    SiteServiceRequestNotFoundError,
    SiteServiceRequestPayloadError,
)

_CLIENT_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_INTERNAL_NOTE_MAX_LENGTH = 20_000
_RETRYABLE_COMMAND_ERROR_CODES = {
    "attachment_download_failed",
    "attachment_write_failed",
    "message_write_failed",
    "support_user_invalid",
}


def _validate_client_request_id(value: str) -> str:
    if _CLIENT_REQUEST_ID_RE.fullmatch(value) is None:
        raise SiteServiceRequestPayloadError("client_request_id_invalid")
    return value


def _command_is_retryable(command: SiteServiceRequestCommand) -> bool:
    return command.status == "failed" and command.last_error_code in _RETRYABLE_COMMAND_ERROR_CODES


def _validate_outgoing_file_metadata(safe_filename: str, mime_type: str) -> None:
    if (
        not safe_filename
        or safe_filename in {".", ".."}
        or "/" in safe_filename
        or "\\" in safe_filename
        or "\x00" in safe_filename
        or len(safe_filename) > 255
    ):
        raise SiteServiceRequestPayloadError("reply_file_name_invalid")
    if not mime_type.strip() or len(mime_type) > 255:
        raise SiteServiceRequestPayloadError("reply_file_type_invalid")


def get_site_service_request_case_for_ui(
    session: Session, *, item_id: int, for_update: bool = False
) -> SiteServiceRequestCase:
    statement = select(SiteServiceRequestCase).where(
        SiteServiceRequestCase.bitrix_item_id == item_id
    )
    if for_update:
        statement = statement.with_for_update()
    case = session.scalar(statement)
    if case is None:
        raise SiteServiceRequestNotFoundError("service_case_not_found")
    return case


def _decrypt_message_text(
    message: SiteServiceRequestMessage, *, cipher: SiteServiceRequestCipher
) -> str | None:
    if message.text_encrypted is None:
        return None
    if message.message_kind == "internal_note":
        aad = f"conversation-note:{message.case_id}:{message.client_request_id}"
    else:
        aad = f"conversation:{message.case_id}:{message.source_message_id}"
    return cipher.decrypt(message.text_encrypted, event_id=aad).decode("utf-8")


def build_site_service_request_conversation(
    session: Session,
    *,
    item_id: int,
    cipher: SiteServiceRequestCipher,
    before_id: int | None = None,
    limit: int = 50,
    site_base_url: str,
) -> dict[str, object]:
    case = get_site_service_request_case_for_ui(session, item_id=item_id)
    statement = select(SiteServiceRequestMessage).where(
        SiteServiceRequestMessage.case_id == case.id
    )
    if before_id is not None:
        statement = statement.where(SiteServiceRequestMessage.id < before_id)
    messages = list(
        session.scalars(
            statement.order_by(
                SiteServiceRequestMessage.created_at.desc(),
                SiteServiceRequestMessage.id.desc(),
            ).limit(limit)
        )
    )
    source_message_ids = {
        message.source_message_id for message in messages if message.source_message_id is not None
    }
    files_by_message: dict[int, list[SiteServiceRequestFile]] = {}
    if source_message_ids:
        for file in session.scalars(
            select(SiteServiceRequestFile).where(
                SiteServiceRequestFile.case_id == case.id,
                SiteServiceRequestFile.source_message_id.in_(source_message_ids),
            )
        ):
            files_by_message.setdefault(file.source_message_id, []).append(file)

    rendered: list[dict[str, object]] = []
    for message in messages:
        if message.direction == "inbound":
            author_label = "Клиент"
            delivery_status = "received"
        elif message.direction == "internal":
            author_label = message.author_name or "Сотрудник"
            delivery_status = "note"
        else:
            author_label = (
                f"{message.author_name} · Поддержка"
                if message.author_name
                else "Поддержка MASTER MOBILE"
            )
            delivery_status = "delivered"
        attachments = [
            {
                "id": f"inbound:{file.id}",
                "name": file.safe_filename,
                "mimeType": file.mime_type,
                "size": file.byte_size,
                "status": file.status,
                "downloadUrl": (
                    f"/api/site-service-requests/ui/items/{item_id}/attachments/{file.id}"
                    if file.bitrix_object_id and file.status == "uploaded"
                    else None
                ),
            }
            for file in files_by_message.get(message.source_message_id or 0, [])
        ]
        rendered.append(
            {
                "id": f"message:{message.id}",
                "direction": message.direction,
                "authorLabel": author_label,
                "text": _decrypt_message_text(message, cipher=cipher),
                "createdAt": message.created_at,
                "deliveryStatus": delivery_status,
                "errorCode": None,
                "retryable": False,
                "visibleToCustomer": message.is_visible_to_customer,
                "attachments": attachments,
            }
        )

    if before_id is None:
        commands = list(
            session.scalars(
                select(SiteServiceRequestCommand)
                .where(SiteServiceRequestCommand.case_id == case.id)
                .order_by(SiteServiceRequestCommand.created_at.desc())
                .limit(50)
            )
        )
        command_source_ids = {
            command.source_message_id
            for command in commands
            if command.source_message_id is not None
        }
        represented_source_ids = set(
            session.scalars(
                select(SiteServiceRequestMessage.source_message_id).where(
                    SiteServiceRequestMessage.case_id == case.id,
                    SiteServiceRequestMessage.source_message_id.in_(command_source_ids),
                )
            )
        )
        for command in commands:
            if command.source_message_id in represented_source_ids:
                continue
            try:
                reply_text = cipher.decrypt(
                    command.reply_encrypted, event_id=command.command_key
                ).decode("utf-8")
            except (
                SiteServiceRequestConfigurationError,
                UnicodeDecodeError,
            ):  # fail closed without taking the whole transcript down
                reply_text = None
            attachment_rows = [
                {
                    "id": f"outbound:{attachment.id}",
                    "name": attachment.safe_filename,
                    "mimeType": attachment.mime_type,
                    "size": attachment.byte_size,
                    "status": attachment.status,
                    "downloadUrl": None,
                }
                for attachment in command.attachments
            ]
            rendered.append(
                {
                    "id": f"command:{command.id}",
                    "direction": "outbound",
                    "authorLabel": (
                        f"{command.created_by_name} · Поддержка"
                        if command.created_by_name
                        else "Поддержка MASTER MOBILE"
                    ),
                    "text": reply_text,
                    "createdAt": command.created_at,
                    "deliveryStatus": (
                        "delivered"
                        if command.status == "applied"
                        else "failed" if command.status == "failed" else "sending"
                    ),
                    "errorCode": command.last_error_code,
                    "retryable": _command_is_retryable(command),
                    "visibleToCustomer": True,
                    "attachments": attachment_rows,
                }
            )

    rendered.sort(key=lambda item: (item["createdAt"], item["id"]))
    return {
        "itemId": item_id,
        "sourceKind": case.source_kind,
        "canReply": case.source_kind == "site_ticket",
        "originalUrl": (
            f"{site_base_url.rstrip('/')}/personal/tickets/?ID={case.source_ticket_id}"
            if case.source_kind == "site_ticket" and case.source_ticket_id > 0
            else None
        ),
        "nextBeforeId": messages[-1].id if len(messages) == limit else None,
        "messages": rendered,
    }


def create_site_service_request_ui_reply(
    session: Session,
    *,
    item_id: int,
    client_request_id: str,
    text: str,
    files: list[tuple[str, str, bytes]],
    actor_user_id: int,
    actor_name: str,
    cipher: SiteServiceRequestCipher,
    max_files: int,
    max_file_bytes: int,
    max_total_file_bytes: int,
    attachments_enabled: bool,
    now: datetime | None = None,
) -> tuple[SiteServiceRequestCommand, bool]:
    client_request_id = _validate_client_request_id(client_request_id)
    normalized = text.strip()
    if not normalized:
        raise SiteServiceRequestPayloadError("reply_text_empty")
    if len(normalized) > SITE_SERVICE_REQUEST_REPLY_MAX_LENGTH:
        raise SiteServiceRequestPayloadError("reply_text_too_long")
    if files and not attachments_enabled:
        raise SiteServiceRequestPayloadError("command_attachments_disabled")
    if len(files) > max_files:
        raise SiteServiceRequestPayloadError("too_many_reply_files")
    if any(len(body) > max_file_bytes for _, _, body in files):
        raise SiteServiceRequestPayloadError("reply_file_too_large")
    if sum(len(body) for _, _, body in files) > max_total_file_bytes:
        raise SiteServiceRequestPayloadError("reply_files_total_too_large")
    for safe_filename, mime_type, _body in files:
        _validate_outgoing_file_metadata(safe_filename, mime_type)

    case = get_site_service_request_case_for_ui(session, item_id=item_id, for_update=True)
    if case.source_kind != "site_ticket":
        raise SiteServiceRequestConflictError("reply_channel_not_supported")
    existing = session.scalar(
        select(SiteServiceRequestCommand).where(
            SiteServiceRequestCommand.case_id == case.id,
            SiteServiceRequestCommand.client_request_id == client_request_id,
        )
    )
    if existing is not None:
        expected_files = [
            (safe_filename, mime_type, len(body), hashlib.sha256(body).hexdigest())
            for safe_filename, mime_type, body in files
        ]
        stored_files = [
            (
                attachment.safe_filename,
                attachment.mime_type,
                attachment.byte_size,
                attachment.sha256,
            )
            for attachment in existing.attachments
        ]
        if (
            existing.reply_sha256 != hashlib.sha256(normalized.encode("utf-8")).hexdigest()
            or stored_files != expected_files
        ):
            raise SiteServiceRequestConflictError("reply_idempotency_conflict")
        return existing, True
    current_time = now or datetime.now(UTC)
    command_key = f"site-support-ui:{case.id}:{client_request_id}"
    reply_bytes = normalized.encode("utf-8")
    command = SiteServiceRequestCommand(
        case_id=case.id,
        command_key=command_key,
        client_request_id=client_request_id,
        created_by_bitrix_user_id=actor_user_id,
        created_by_name=actor_name,
        reply_encrypted=cipher.encrypt(reply_bytes, event_id=command_key),
        reply_sha256=hashlib.sha256(reply_bytes).hexdigest(),
        status="pending",
        created_at=current_time,
        updated_at=current_time,
    )
    session.add(command)
    session.flush()
    for index, (safe_filename, mime_type, body) in enumerate(files, start=1):
        request_digest = hashlib.sha256(client_request_id.encode("ascii")).hexdigest()
        client_file_id = f"{index}-{request_digest[:48]}"
        session.add(
            SiteServiceRequestCommandFile(
                command_id=command.id,
                client_file_id=client_file_id,
                safe_filename=safe_filename,
                mime_type=mime_type,
                byte_size=len(body),
                sha256=hashlib.sha256(body).hexdigest(),
                payload_encrypted=cipher.encrypt(
                    body, event_id=f"command-file:{command_key}:{client_file_id}"
                ),
                status="pending",
                created_at=current_time,
                updated_at=current_time,
            )
        )
    case.updated_at = current_time
    session.flush()
    return command, False


def create_site_service_request_internal_note(
    session: Session,
    *,
    item_id: int,
    client_request_id: str,
    text: str,
    actor_user_id: int,
    actor_name: str,
    cipher: SiteServiceRequestCipher,
    now: datetime | None = None,
) -> tuple[SiteServiceRequestMessage, bool]:
    client_request_id = _validate_client_request_id(client_request_id)
    normalized = text.strip()
    if not normalized:
        raise SiteServiceRequestPayloadError("note_text_empty")
    if len(normalized) > _INTERNAL_NOTE_MAX_LENGTH:
        raise SiteServiceRequestPayloadError("note_text_too_long")
    case = get_site_service_request_case_for_ui(session, item_id=item_id, for_update=True)
    existing = session.scalar(
        select(SiteServiceRequestMessage).where(
            SiteServiceRequestMessage.case_id == case.id,
            SiteServiceRequestMessage.client_request_id == client_request_id,
        )
    )
    if existing is not None:
        if existing.text_sha256 != hashlib.sha256(normalized.encode("utf-8")).hexdigest():
            raise SiteServiceRequestConflictError("note_idempotency_conflict")
        return existing, True
    current_time = now or datetime.now(UTC)
    raw = normalized.encode("utf-8")
    message = SiteServiceRequestMessage(
        case_id=case.id,
        client_request_id=client_request_id,
        message_kind="internal_note",
        direction="internal",
        author_kind="support",
        author_bitrix_user_id=actor_user_id,
        author_name=actor_name,
        is_visible_to_customer=False,
        text_encrypted=cipher.encrypt(
            raw, event_id=f"conversation-note:{case.id}:{client_request_id}"
        ),
        text_sha256=hashlib.sha256(raw).hexdigest(),
        created_at=current_time,
        updated_at=current_time,
    )
    session.add(message)
    session.flush()
    return message, False


def retry_site_service_request_ui_command(
    session: Session,
    *,
    item_id: int,
    command_id: int,
    now: datetime | None = None,
) -> SiteServiceRequestCommand:
    case = get_site_service_request_case_for_ui(session, item_id=item_id, for_update=True)
    command = session.scalar(
        select(SiteServiceRequestCommand)
        .where(
            SiteServiceRequestCommand.id == command_id,
            SiteServiceRequestCommand.case_id == case.id,
        )
        .with_for_update()
    )
    if command is None:
        raise SiteServiceRequestNotFoundError("reply_command_not_found")
    if not _command_is_retryable(command):
        raise SiteServiceRequestConflictError("reply_command_not_retryable")
    current_time = now or datetime.now(UTC)
    command.status = "pending"
    command.lease_until = None
    command.lease_token = None
    command.last_error_code = None
    command.updated_at = current_time
    for attachment in command.attachments:
        if attachment.status == "failed":
            attachment.status = "pending"
            attachment.last_error_code = None
            attachment.updated_at = current_time
    return command
