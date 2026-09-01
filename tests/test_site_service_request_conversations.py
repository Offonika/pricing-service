from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.core.config import Settings
from app.models.site_service_requests import (
    SiteServiceRequestCase,
    SiteServiceRequestCommand,
    SiteServiceRequestCommandFile,
    SiteServiceRequestMessage,
)
from app.schemas.site_service_requests import SiteServiceRequestEventPayload
from app.services.site_service_request_conversations import (
    build_site_service_request_conversation,
    create_site_service_request_internal_note,
    create_site_service_request_ui_reply,
)
from app.services.site_service_requests import (
    SiteServiceRequestCipher,
    SiteServiceRequestConflictError,
    accept_site_service_request_event,
    lease_site_service_request_commands,
    purge_expired_site_service_request_conversations,
    reconcile_site_service_request_conversation_snapshot,
)
from app.services.site_service_requests_ui_auth import (
    authenticate_site_service_request_ui_user,
    create_site_service_request_ui_session_token,
    verify_site_service_request_ui_session_token,
)


def _cipher() -> SiteServiceRequestCipher:
    return SiteServiceRequestCipher(base64.urlsafe_b64encode(b"c" * 32).decode())


def _payload(*, closed: bool = False) -> SiteServiceRequestEventPayload:
    return SiteServiceRequestEventPayload.model_validate(
        {
            "schemaVersion": 1,
            "eventId": "site-support:760:1782",
            "eventType": "message.created",
            "occurredAt": "2026-08-30T10:05:00+03:00",
            "ticket": {
                "id": 760,
                "siteId": "s1",
                "ownerUserId": 100,
                "title": "Проверка переписки",
                "phone": "+70000003223",
                "email": None,
                "orderNumber": "3223",
                "requestType": "consultation",
                "isClosed": closed,
            },
            "history": [
                {
                    "messageId": 1780,
                    "authorKind": "customer",
                    "isVisibleToCustomer": True,
                    "createdAt": "2026-08-30T10:00:00+03:00",
                    "text": "Когда будет ответ?",
                    "files": [],
                },
                {
                    "messageId": 1782,
                    "authorKind": "support-team",
                    "isVisibleToCustomer": True,
                    "createdAt": "2026-08-30T10:05:00+03:00",
                    "text": "Обращение принято.",
                    "files": [],
                },
            ],
        }
    )


def test_event_builds_encrypted_conversation_read_model(db_session):
    payload = _payload()
    raw = json.dumps(payload.model_dump(mode="json", by_alias=True)).encode()
    accept_site_service_request_event(
        db_session,
        payload=payload,
        raw_body=raw,
        payload_sha256="a" * 64,
        cipher=_cipher(),
        max_file_bytes=10 * 1024 * 1024,
    )
    db_session.commit()

    case = db_session.scalar(select(SiteServiceRequestCase))
    assert case is not None
    case.bitrix_item_id = 391
    db_session.commit()
    rows = list(db_session.scalars(select(SiteServiceRequestMessage)))
    assert [row.source_message_id for row in rows] == [1780, 1782]
    marker = "Обращение".encode()
    assert all(row.text_encrypted and marker not in row.text_encrypted for row in rows)

    conversation = build_site_service_request_conversation(
        db_session,
        item_id=391,
        cipher=_cipher(),
        site_base_url="https://master-mobile.ru",
    )
    assert [item["text"] for item in conversation["messages"]] == [
        "Когда будет ответ?",
        "Обращение принято.",
    ]


def test_ui_reply_note_idempotency_and_file_capability(db_session):
    case = SiteServiceRequestCase(
        source_ticket_id=760,
        source_kind="site_ticket",
        source_key="site-support-ticket:760",
        bitrix_item_id=391,
        first_seen_at=datetime.now(UTC),
    )
    db_session.add(case)
    db_session.commit()
    cipher = _cipher()

    command, duplicate = create_site_service_request_ui_reply(
        db_session,
        item_id=391,
        client_request_id="request-3223",
        text="Ответ клиенту",
        files=[("answer.pdf", "application/pdf", b"pdf")],
        actor_user_id=131016,
        actor_name="Тимур Тибилов",
        cipher=cipher,
        max_files=5,
        max_file_bytes=10 * 1024 * 1024,
        max_total_file_bytes=20 * 1024 * 1024,
        attachments_enabled=True,
    )
    same, duplicate_again = create_site_service_request_ui_reply(
        db_session,
        item_id=391,
        client_request_id="request-3223",
        text="Ответ клиенту",
        files=[("answer.pdf", "application/pdf", b"pdf")],
        actor_user_id=131016,
        actor_name="Тимур Тибилов",
        cipher=cipher,
        max_files=5,
        max_file_bytes=10 * 1024 * 1024,
        max_total_file_bytes=20 * 1024 * 1024,
        attachments_enabled=True,
    )
    assert not duplicate
    assert duplicate_again and same.id == command.id
    with pytest.raises(SiteServiceRequestConflictError, match="reply_idempotency_conflict"):
        create_site_service_request_ui_reply(
            db_session,
            item_id=391,
            client_request_id="request-3223",
            text="Другой ответ",
            files=[("answer.pdf", "application/pdf", b"pdf")],
            actor_user_id=131016,
            actor_name="Тимур Тибилов",
            cipher=cipher,
            max_files=5,
            max_file_bytes=10 * 1024 * 1024,
            max_total_file_bytes=20 * 1024 * 1024,
            attachments_enabled=True,
        )
    assert (
        lease_site_service_request_commands(
            db_session, cipher=cipher, enabled=True, lease_seconds=300
        )
        == []
    )
    leased = lease_site_service_request_commands(
        db_session,
        cipher=cipher,
        enabled=True,
        lease_seconds=300,
        include_attachments=True,
    )
    assert len(leased) == 1 and leased[0].files[0].sha256

    note, note_duplicate = create_site_service_request_internal_note(
        db_session,
        item_id=391,
        client_request_id="note-3223",
        text="Только для коллег",
        actor_user_id=131016,
        actor_name="Тимур Тибилов",
        cipher=cipher,
    )
    assert not note_duplicate and not note.is_visible_to_customer
    with pytest.raises(SiteServiceRequestConflictError, match="note_idempotency_conflict"):
        create_site_service_request_internal_note(
            db_session,
            item_id=391,
            client_request_id="note-3223",
            text="Другой текст заметки",
            actor_user_id=131016,
            actor_name="Тимур Тибилов",
            cipher=cipher,
        )


def test_closed_conversation_is_purged_and_can_be_rebuilt(db_session):
    payload = _payload(closed=True)
    raw = json.dumps(payload.model_dump(mode="json", by_alias=True)).encode()
    now = datetime(2026, 8, 30, 8, tzinfo=UTC)
    accept_site_service_request_event(
        db_session,
        payload=payload,
        raw_body=raw,
        payload_sha256="b" * 64,
        cipher=_cipher(),
        max_file_bytes=10 * 1024 * 1024,
        conversation_retention_days=90,
        now=now,
    )
    db_session.commit()
    assert (
        purge_expired_site_service_request_conversations(db_session, now=now + timedelta(days=91))
        == 1
    )
    assert all(
        row.text_encrypted is None for row in db_session.scalars(select(SiteServiceRequestMessage))
    )


def test_stale_snapshot_cannot_change_conversation_close_state(db_session):
    payload = _payload()
    raw = json.dumps(payload.model_dump(mode="json", by_alias=True)).encode()
    accept_site_service_request_event(
        db_session,
        payload=payload,
        raw_body=raw,
        payload_sha256="c" * 64,
        cipher=_cipher(),
        max_file_bytes=10 * 1024 * 1024,
    )
    case = db_session.scalar(select(SiteServiceRequestCase))
    assert case is not None
    hidden = payload.model_copy(
        update={
            "history": [
                payload.history[0],
                payload.history[1].model_copy(update={"is_visible_to_customer": False}),
            ]
        }
    )
    reconcile_site_service_request_conversation_snapshot(
        db_session, case=case, payload=hidden, cipher=_cipher()
    )
    hidden_message = db_session.scalar(
        select(SiteServiceRequestMessage).where(SiteServiceRequestMessage.source_message_id == 1782)
    )
    assert hidden_message is not None and hidden_message.direction == "internal"
    stale = payload.model_copy(
        update={
            "ticket": payload.ticket.model_copy(update={"is_closed": True}),
            "history": payload.history[:1],
        }
    )
    reconcile_site_service_request_conversation_snapshot(
        db_session, case=case, payload=stale, cipher=_cipher()
    )
    assert case.conversation_closed_at is None
    assert case.conversation_purge_after is None


def test_purge_waits_for_active_command_and_removes_terminal_payload(db_session):
    now = datetime(2026, 8, 30, 8, tzinfo=UTC)
    case = SiteServiceRequestCase(
        source_ticket_id=760,
        source_kind="site_ticket",
        source_key="site-support-ticket:760",
        bitrix_item_id=391,
        first_seen_at=now,
        conversation_closed_at=now - timedelta(days=91),
        conversation_purge_after=now - timedelta(days=1),
    )
    db_session.add(case)
    db_session.flush()
    command = SiteServiceRequestCommand(
        case_id=case.id,
        command_key="site-support-ui:1:request-3223",
        client_request_id="request-3223",
        reply_encrypted=b"encrypted",
        reply_sha256="a" * 64,
        status="pending",
        created_at=now,
        updated_at=now,
    )
    db_session.add(command)
    db_session.flush()

    assert purge_expired_site_service_request_conversations(db_session, now=now) == 0
    assert case.conversation_purge_after is not None
    command.status = "failed"
    assert purge_expired_site_service_request_conversations(db_session, now=now) == 1
    db_session.flush()
    assert db_session.get(SiteServiceRequestCommand, command.id) is None


def test_corrupt_command_attachment_does_not_mask_valid_encryption_key(db_session):
    now = datetime(2026, 8, 30, 8, tzinfo=UTC)
    case = SiteServiceRequestCase(
        source_ticket_id=760,
        source_kind="site_ticket",
        source_key="site-support-ticket:760",
        bitrix_item_id=391,
        first_seen_at=now,
    )
    db_session.add(case)
    db_session.flush()
    cipher = _cipher()
    reply = b"valid reply"
    command = SiteServiceRequestCommand(
        case_id=case.id,
        command_key="site-support-ui:1:request-3223",
        client_request_id="request-3223",
        reply_encrypted=cipher.encrypt(reply, event_id="site-support-ui:1:request-3223"),
        reply_sha256=hashlib.sha256(reply).hexdigest(),
        status="pending",
        created_at=now,
        updated_at=now,
    )
    db_session.add(command)
    db_session.flush()
    db_session.add(
        SiteServiceRequestCommandFile(
            command_id=command.id,
            client_file_id="file-request-3223",
            safe_filename="answer.txt",
            mime_type="text/plain",
            byte_size=3,
            sha256="a" * 64,
            payload_encrypted=b"corrupt",
            status="pending",
            created_at=now,
            updated_at=now,
        )
    )
    db_session.flush()

    assert (
        lease_site_service_request_commands(
            db_session,
            cipher=cipher,
            enabled=True,
            lease_seconds=300,
            include_attachments=True,
        )
        == []
    )
    assert command.status == "failed"
    assert command.last_error_code == "command_payload_invalid"


def test_ui_session_is_item_scoped_and_signed():
    settings = Settings(
        site_service_requests_ui_enabled=True,
        site_service_requests_ui_session_secret="test-ui-session-secret-at-least-32-bytes",
        site_service_requests_ui_allowed_domains="portal.example",
        site_service_requests_ui_allowed_member_ids="member-3223",
    )
    token, _expires = create_site_service_request_ui_session_token(
        domain="portal.example",
        member_id="member-3223",
        user_id=131016,
        user_name="Тимур Тибилов",
        is_admin=False,
        item_id=391,
        settings=settings,
        now=1_787_000_000,
    )
    session = verify_site_service_request_ui_session_token(
        token, settings=settings, now=1_787_000_001
    )
    assert session.item_id == 391
    assert session.user_id == 131016


def test_ui_allowed_user_ids_reject_fractional_values():
    with pytest.raises(ValueError, match="positive integers"):
        Settings(site_service_requests_ui_allowed_user_ids=[131016.5])

    with pytest.raises(ValueError, match="positive integers"):
        Settings(site_service_requests_ui_write_allowed_user_ids=[115204.5])


def test_ui_auth_rejects_malformed_bitrix_user_name(monkeypatch):
    settings = Settings(site_service_requests_ui_allowed_user_ids=[131016])
    responses = iter(
        [
            {"ID": "131016", "NAME": ["Тимур"], "LAST_NAME": "Тибилов"},
            {"item": {"id": 391}},
        ]
    )
    monkeypatch.setattr(
        "app.services.site_service_requests_ui_auth._bitrix_call",
        lambda **_kwargs: next(responses),
    )
    with pytest.raises(HTTPException) as exc_info:
        authenticate_site_service_request_ui_user(
            domain="portal.example",
            access_token="token",
            item_id=391,
            settings=settings,
        )
    assert exc_info.value.detail == "bitrix_user_payload_invalid"
