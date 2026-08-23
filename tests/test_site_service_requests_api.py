from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import stat
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from urllib.parse import quote
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.api.dependencies import (
    SiteServiceRequestBodyLimitMiddleware,
    get_db,
    get_site_service_request_settings,
)
from app.core.config import Settings
from app.main import app
from app.models.site_service_requests import (
    SiteServiceRequestCase,
    SiteServiceRequestCommand,
    SiteServiceRequestEvent,
    SiteServiceRequestFile,
    SiteServiceRequestNonce,
)
from app.schemas.site_service_requests import SiteServiceRequestCommandAckPayload
from app.services.site_service_requests import (
    SiteServiceRequestCipher,
    acknowledge_site_service_request_command,
)
from app.services.site_service_requests_auth import content_sha256, sign_site_request

_SECRET = "test-only-site-service-request-hmac-secret"
_ENCRYPTION_KEY = base64.urlsafe_b64encode(b"k" * 32).decode("ascii")
_EVENT_PATH = "/api/internal/site-service-requests/events"
_COMMANDS_PATH = "/api/internal/site-service-requests/commands"
_HEALTH_PATH = "/api/internal/site-service-requests/health"


def _settings(**overrides) -> Settings:
    values = {
        "site_service_requests_ingest_enabled": True,
        "site_service_requests_bitrix_writes_enabled": False,
        "site_service_requests_outbound_replies_enabled": False,
        "site_service_requests_hmac_secret": _SECRET,
        "site_service_requests_event_encryption_key": _ENCRYPTION_KEY,
    }
    values.update(overrides)
    return Settings(**values)


def _event_payload() -> dict:
    return {
        "schemaVersion": 1,
        "eventId": "site-support:741:1201",
        "eventType": "ticket.created",
        "occurredAt": "2026-08-22T12:00:00+03:00",
        "ticket": {
            "id": 741,
            "siteId": "s1",
            "ownerUserId": 123,
            "title": "Не работает устройство",
            "phone": "+70000000000",
            "email": "client@example.invalid",
            "orderNumber": "000001",
            "requestType": "warranty",
            "isClosed": False,
        },
        "history": [
            {
                "messageId": 1201,
                "authorKind": "customer",
                "createdAt": "2026-08-22T12:00:00+03:00",
                "text": "Описание проблемы",
                "files": [
                    {
                        "fileId": 93287,
                        "name": "photo.jpg",
                        "mimeType": "image/jpeg",
                        "size": 1024,
                        "sha256": "a" * 64,
                    }
                ],
            }
        ],
    }


def _json_body(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _signed_headers(
    *,
    method: str,
    path: str,
    body: bytes,
    nonce: str | None = None,
    content_type: str = "application/json",
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    timestamp = int(datetime.now(UTC).timestamp())
    nonce_value = nonce or str(uuid4())
    digest = content_sha256(body)
    headers = {
        "Content-Type": content_type,
        "X-MM-Site-Timestamp": str(timestamp),
        "X-MM-Site-Nonce": nonce_value,
        "X-MM-Site-Content-SHA256": digest,
        "X-MM-Site-Signature": sign_site_request(
            secret=_SECRET,
            timestamp=timestamp,
            nonce=nonce_value,
            method=method,
            path=path,
            body_sha256=digest,
        ),
    }
    headers.update(extra or {})
    return headers


@contextmanager
def _api_dependencies(db_session, settings: Settings):
    def override_db():
        yield db_session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_site_service_request_settings] = lambda: settings
    try:
        yield
    finally:
        app.dependency_overrides.clear()


def _post_event(client, payload: dict, *, nonce: str | None = None):
    body = _json_body(payload)
    return client.post(
        _EVENT_PATH,
        content=body,
        headers=_signed_headers(
            method="POST",
            path=_EVENT_PATH,
            body=body,
            nonce=nonce,
        ),
    )


def _put_file(
    client,
    *,
    event_id: str,
    file_id: int,
    body: bytes,
    filename: str = "photo.jpg",
    mime_type: str = "image/jpeg",
    content_disposition: str | None = None,
):
    path = f"{_EVENT_PATH}/{event_id}/files/{file_id}"
    return client.put(
        path,
        content=body,
        headers=_signed_headers(
            method="PUT",
            path=path,
            body=body,
            content_type=mime_type,
            extra={
                "Content-Disposition": (
                    content_disposition or f'attachment; filename="{filename}"'
                ),
                "Content-Length": str(len(body)),
            },
        ),
    )


def _report_file_unavailable(
    client,
    *,
    event_id: str,
    file_id: int,
):
    path = f"{_EVENT_PATH}/{event_id}/files/{file_id}"
    return client.put(
        path,
        content=b"",
        headers=_signed_headers(
            method="PUT",
            path=path,
            body=b"",
            content_type="application/octet-stream",
            extra={
                "Content-Length": "0",
                "X-MM-Site-File-Error": "file_unavailable",
            },
        ),
    )


def _get_commands(client):
    return client.get(
        _COMMANDS_PATH,
        headers=_signed_headers(method="GET", path=_COMMANDS_PATH, body=b""),
    )


def _post_command_ack(client, command_id: int, payload: dict):
    path = f"{_COMMANDS_PATH}/{command_id}/ack"
    body = _json_body(payload)
    return client.post(
        path,
        content=body,
        headers=_signed_headers(method="POST", path=path, body=body),
    )


def _create_command(
    db_session,
    *,
    case_id: int,
    command_key: str = "site-support-reply:741:1",
    reply_text: str = "Проверочный ответ клиенту",
) -> SiteServiceRequestCommand:
    reply = reply_text.encode("utf-8")
    command = SiteServiceRequestCommand(
        case_id=case_id,
        command_key=command_key,
        reply_encrypted=SiteServiceRequestCipher(_ENCRYPTION_KEY).encrypt(
            reply,
            event_id=command_key,
        ),
        reply_sha256=hashlib.sha256(reply).hexdigest(),
        status="pending",
    )
    db_session.add(command)
    db_session.commit()
    return command


def test_event_api_accepts_encrypted_payload_and_returns_missing_files(
    client,
    db_session,
) -> None:
    payload = _event_payload()
    body = _json_body(payload)
    with _api_dependencies(db_session, _settings()):
        response = _post_event(client, payload)

    assert response.status_code == 202
    assert response.json() == {
        "eventId": "site-support:741:1201",
        "status": "accepted",
        "duplicate": False,
        "missingFileIds": [93287],
    }
    case = db_session.scalar(select(SiteServiceRequestCase))
    event = db_session.scalar(select(SiteServiceRequestEvent))
    file = db_session.scalar(select(SiteServiceRequestFile))
    assert case is not None and case.source_ticket_id == 741
    assert case.latest_inbound_message_id == 1201
    assert event is not None and event.payload_encrypted is not None
    assert event.source_message_sha256 is not None
    assert len(event.source_message_sha256) == 64
    assert body not in event.payload_encrypted
    assert (
        SiteServiceRequestCipher(_ENCRYPTION_KEY).decrypt(
            event.payload_encrypted,
            event_id=event.event_id,
        )
        == body
    )
    assert file is not None and file.temporary_path is None
    assert db_session.scalar(select(func.count(SiteServiceRequestNonce.id))) == 1


def test_event_api_returns_idempotent_duplicate_without_extra_rows(client, db_session) -> None:
    payload = _event_payload()
    with _api_dependencies(db_session, _settings()):
        first = _post_event(client, payload)
        second = _post_event(client, payload)

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["duplicate"] is True
    assert second.json()["missingFileIds"] == [93287]
    assert db_session.scalar(select(func.count(SiteServiceRequestCase.id))) == 1
    assert db_session.scalar(select(func.count(SiteServiceRequestEvent.id))) == 1
    assert db_session.scalar(select(func.count(SiteServiceRequestFile.id))) == 1
    assert db_session.scalar(select(func.count(SiteServiceRequestNonce.id))) == 2


def test_event_api_accepts_changed_ticket_metadata_for_same_source_message(
    client,
    db_session,
) -> None:
    original = _event_payload()
    changed = _event_payload()
    changed["ticket"]["title"] = "Другой текст"
    with _api_dependencies(db_session, _settings()):
        assert _post_event(client, original).status_code == 202
        duplicate = _post_event(client, changed)

    assert duplicate.status_code == 202
    assert duplicate.json()["duplicate"] is True
    assert db_session.scalar(select(func.count(SiteServiceRequestEvent.id))) == 1
    event = db_session.scalar(select(SiteServiceRequestEvent))
    assert event is not None
    assert event.payload_sha256 == content_sha256(_json_body(original))


def test_event_api_rejects_changed_source_message_for_same_event_id(
    client,
    db_session,
) -> None:
    original = _event_payload()
    changed = _event_payload()
    changed["history"][0]["text"] = "Другой текст исходного сообщения"
    with _api_dependencies(db_session, _settings()):
        assert _post_event(client, original).status_code == 202
        conflict = _post_event(client, changed)

    assert conflict.status_code == 409
    assert conflict.json() == {"detail": "event_payload_conflict"}


def test_event_api_rejects_nonce_replay_before_idempotent_event_check(
    client,
    db_session,
) -> None:
    nonce = str(uuid4())
    payload = _event_payload()
    with _api_dependencies(db_session, _settings()):
        first = _post_event(client, payload, nonce=nonce)
        replay = _post_event(client, payload, nonce=nonce)

    assert first.status_code == 202
    assert replay.status_code == 409
    assert replay.json() == {"detail": "nonce_replay"}
    assert db_session.scalar(select(func.count(SiteServiceRequestEvent.id))) == 1


def test_bearer_token_does_not_replace_site_hmac(client, db_session) -> None:
    with _api_dependencies(db_session, _settings()):
        response = client.post(
            _EVENT_PATH,
            json=_event_payload(),
            headers={"Authorization": "Bearer another-contour-token"},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "unauthorized"}
    assert db_session.scalar(select(func.count(SiteServiceRequestNonce.id))) == 0


def test_event_api_keeps_ingest_disabled_and_encryption_fail_closed(
    client,
    db_session,
) -> None:
    with _api_dependencies(
        db_session,
        _settings(site_service_requests_ingest_enabled=False),
    ):
        disabled = _post_event(client, _event_payload())
    assert disabled.status_code == 503
    assert disabled.json() == {"detail": "ingest_disabled"}
    assert db_session.scalar(select(func.count(SiteServiceRequestEvent.id))) == 0

    with _api_dependencies(
        db_session,
        _settings(site_service_requests_event_encryption_key=None),
    ):
        unconfigured = _post_event(client, _event_payload())
    assert unconfigured.status_code == 503
    assert unconfigured.json() == {"detail": "event_encryption_not_configured"}
    assert db_session.scalar(select(func.count(SiteServiceRequestEvent.id))) == 0


def test_event_api_preserves_case_when_file_metadata_is_oversized(client, db_session) -> None:
    payload = _event_payload()
    payload["history"][0]["files"][0]["size"] = 10 * 1024 * 1024 + 1
    with _api_dependencies(db_session, _settings()):
        response = _post_event(client, payload)

    assert response.status_code == 202
    assert response.json()["missingFileIds"] == []
    case = db_session.scalar(select(SiteServiceRequestCase))
    event = db_session.scalar(select(SiteServiceRequestEvent))
    file = db_session.scalar(select(SiteServiceRequestFile))
    assert case is not None and case.sync_status == "file_sync_error"
    assert case.last_error_code == "file_too_large"
    assert event is not None and event.status == "pending"
    assert file is not None and file.status == "failed"
    assert file.last_error_code == "file_too_large"


def test_event_api_rejects_body_over_configured_limit_without_reserving_nonce(
    client,
    db_session,
) -> None:
    payload = _event_payload()
    payload["history"][0]["text"] = "x" * 2048
    with _api_dependencies(
        db_session,
        _settings(site_service_requests_max_event_body_bytes=1024),
    ):
        response = _post_event(client, payload)

    assert response.status_code == 413
    assert response.json() == {"detail": "request_body_too_large"}
    assert db_session.scalar(select(func.count(SiteServiceRequestNonce.id))) == 0
    assert db_session.scalar(select(func.count(SiteServiceRequestEvent.id))) == 0


def test_streaming_body_limit_stops_chunked_request_without_content_length() -> None:
    consumed_all = False
    sent: list[dict] = []
    chunks = [
        {"type": "http.request", "body": b"a" * 600, "more_body": True},
        {"type": "http.request", "body": b"b" * 600, "more_body": True},
        {"type": "http.request", "body": b"c" * 600, "more_body": False},
    ]

    async def downstream(_scope, receive, _send) -> None:
        nonlocal consumed_all
        while True:
            message = await receive()
            if not message.get("more_body"):
                consumed_all = True
                return

    async def receive() -> dict:
        return chunks.pop(0)

    async def send(message: dict) -> None:
        sent.append(message)

    middleware = SiteServiceRequestBodyLimitMiddleware(
        downstream,
        settings=_settings(site_service_requests_max_event_body_bytes=1024),
    )
    asyncio.run(
        middleware(
            {
                "type": "http",
                "method": "POST",
                "path": _EVENT_PATH,
                "headers": [],
            },
            receive,
            send,
        )
    )

    assert consumed_all is False
    assert len(chunks) == 1
    assert sent[0]["status"] == 413


def test_real_app_returns_413_for_chunked_oversized_json(client) -> None:
    body = json.dumps(
        {"padding": "x" * (4 * 1024 * 1024)},
        separators=(",", ":"),
    ).encode("utf-8")
    headers = _signed_headers(method="POST", path=_EVENT_PATH, body=body)
    headers.pop("Content-Length", None)

    response = client.post(
        _EVENT_PATH,
        content=iter((body[: 2 * 1024 * 1024], body[2 * 1024 * 1024 :])),
        headers=headers,
    )

    assert response.status_code == 413
    assert response.json() == {"detail": "request_body_too_large"}


def test_command_ack_api_rejects_body_over_configured_limit_without_reserving_nonce(
    client,
    db_session,
) -> None:
    payload = {
        "schemaVersion": 1,
        "status": "failed",
        "errorCode": "message_write_failed",
        "padding": "x" * 2048,
    }
    with _api_dependencies(
        db_session,
        _settings(site_service_requests_max_ack_body_bytes=1024),
    ):
        response = _post_command_ack(client, 999, payload)

    assert response.status_code == 413
    assert response.json() == {"detail": "request_body_too_large"}
    assert db_session.scalar(select(func.count(SiteServiceRequestNonce.id))) == 0


def test_file_api_stages_binary_idempotently_and_stops_requesting_it(
    client,
    db_session,
    tmp_path,
) -> None:
    body = b"binary-photo-content"
    payload = _event_payload()
    payload["history"][0]["files"][0]["size"] = len(body)
    payload["history"][0]["files"][0]["sha256"] = hashlib.sha256(body).hexdigest()
    settings = _settings(site_service_requests_file_spool_dir=str(tmp_path))

    with _api_dependencies(db_session, settings):
        assert _post_event(client, payload).status_code == 202
        staged = _put_file(
            client,
            event_id=payload["eventId"],
            file_id=93287,
            body=body,
        )
        duplicate = _put_file(
            client,
            event_id=payload["eventId"],
            file_id=93287,
            body=body,
        )
        repeated_event = _post_event(client, payload)

    assert staged.status_code == 200
    assert staged.json() == {
        "eventId": payload["eventId"],
        "fileId": 93287,
        "status": "staged",
        "duplicate": False,
    }
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True
    assert repeated_event.status_code == 202
    assert repeated_event.json()["missingFileIds"] == []

    file = db_session.scalar(select(SiteServiceRequestFile))
    assert file is not None and file.status == "staged"
    assert file.temporary_path is not None
    stored_path = tmp_path / str(file.case_id) / f"{file.id}.bin"
    assert file.temporary_path == str(stored_path.resolve())
    assert stored_path.read_bytes() == body
    assert "photo.jpg" not in file.temporary_path
    assert stat.S_IMODE(stored_path.stat().st_mode) == 0o600


def test_file_api_preserves_rfc5987_unicode_filename(client, db_session, tmp_path) -> None:
    body = b"unicode-file-name"
    filename = "фото устройства.jpg"
    payload = _event_payload()
    payload["history"][0]["files"][0].update(
        {
            "name": filename,
            "size": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
        }
    )
    settings = _settings(site_service_requests_file_spool_dir=str(tmp_path))

    with _api_dependencies(db_session, settings):
        assert _post_event(client, payload).status_code == 202
        uploaded = _put_file(
            client,
            event_id=payload["eventId"],
            file_id=93287,
            body=body,
            content_disposition=f"attachment; filename*=UTF-8''{quote(filename)}",
        )

    assert uploaded.status_code == 200
    file = db_session.scalar(select(SiteServiceRequestFile))
    assert file is not None and file.safe_filename == filename


def test_file_unavailable_is_durable_and_zero_hash_placeholder_can_recover(
    client,
    db_session,
    tmp_path,
) -> None:
    body = b"recovered-file"
    payload = _event_payload()
    payload["history"][0]["files"][0].update(
        {
            "name": "attachment-93287.bin",
            "mimeType": "application/octet-stream",
            "size": 0,
            "sha256": "0" * 64,
        }
    )
    recovered_payload = json.loads(json.dumps(payload))
    recovered_payload["history"][0]["files"][0].update(
        {
            "name": "photo.jpg",
            "mimeType": "image/jpeg",
            "size": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
        }
    )
    settings = _settings(site_service_requests_file_spool_dir=str(tmp_path))

    with _api_dependencies(db_session, settings):
        accepted = _post_event(client, payload)
        failed = _report_file_unavailable(
            client,
            event_id=payload["eventId"],
            file_id=93287,
        )
        file = db_session.scalar(select(SiteServiceRequestFile))
        assert file is not None
        reported_at = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
        file.bitrix_error_reported_at = reported_at
        db_session.commit()
        duplicate_failure = _report_file_unavailable(
            client,
            event_id=payload["eventId"],
            file_id=93287,
        )
        db_session.refresh(file)
        assert file.bitrix_error_reported_at is not None
        assert file.bitrix_error_reported_at.replace(tzinfo=UTC) == reported_at
        recovered_event = _post_event(client, recovered_payload)
        recovered = _put_file(
            client,
            event_id=payload["eventId"],
            file_id=93287,
            body=body,
        )

    assert accepted.status_code == 202
    assert failed.status_code == 200
    assert failed.json() == {
        "eventId": payload["eventId"],
        "fileId": 93287,
        "status": "failed",
        "duplicate": False,
        "errorCode": "file_unavailable",
    }
    assert duplicate_failure.status_code == 200
    assert duplicate_failure.json()["duplicate"] is True
    assert recovered_event.status_code == 202
    assert recovered_event.json()["duplicate"] is True
    assert recovered_event.json()["missingFileIds"] == [93287]
    assert recovered.status_code == 200
    file = db_session.scalar(select(SiteServiceRequestFile))
    assert file is not None
    assert file.status == "staged"
    assert file.safe_filename == "photo.jpg"
    assert file.mime_type == "image/jpeg"
    assert file.byte_size == len(body)
    assert file.sha256 == hashlib.sha256(body).hexdigest()
    assert file.last_error_code is None
    assert file.case.sync_status == file.case.base_sync_status


def test_later_history_snapshot_does_not_consume_recoverable_file_placeholder(
    client,
    db_session,
    tmp_path,
) -> None:
    body = b"restored-after-later-message"
    first = _event_payload()
    first["history"][0]["files"][0].update(
        {
            "name": "attachment-93287.bin",
            "mimeType": "application/octet-stream",
            "size": 0,
            "sha256": "0" * 64,
        }
    )
    recovered_first = json.loads(json.dumps(first))
    recovered_first["history"][0]["files"][0].update(
        {
            "name": "фото.jpg",
            "mimeType": "image/jpeg",
            "size": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
        }
    )
    later = json.loads(json.dumps(recovered_first))
    later["eventId"] = "site-support:741:1301"
    later["eventType"] = "ticket.message_added"
    later["occurredAt"] = "2026-08-22T12:01:00+03:00"
    later["history"].append(
        {
            "messageId": 1301,
            "authorKind": "customer",
            "createdAt": "2026-08-22T12:01:00+03:00",
            "text": "Файл восстановлен",
            "files": [],
        }
    )
    settings = _settings(site_service_requests_file_spool_dir=str(tmp_path))

    with _api_dependencies(db_session, settings):
        assert _post_event(client, first).status_code == 202
        assert (
            _report_file_unavailable(
                client,
                event_id=first["eventId"],
                file_id=93287,
            ).status_code
            == 200
        )
        assert _post_event(client, later).status_code == 202
        placeholder = db_session.scalar(
            select(SiteServiceRequestFile).where(
                SiteServiceRequestFile.source_message_id == 1201
            )
        )
        assert placeholder is not None
        assert placeholder.sha256 == "0" * 64
        assert placeholder.last_error_code == "file_unavailable"

        repeated = _post_event(client, recovered_first)
        uploaded = _put_file(
            client,
            event_id=first["eventId"],
            file_id=93287,
            body=body,
            content_disposition=f"attachment; filename*=UTF-8''{quote('фото.jpg')}",
        )

    assert repeated.status_code == 202
    assert repeated.json()["missingFileIds"] == [93287]
    assert uploaded.status_code == 200
    db_session.refresh(placeholder)
    assert placeholder.status == "staged"
    assert placeholder.safe_filename == "фото.jpg"
    assert placeholder.sha256 == hashlib.sha256(body).hexdigest()


def test_file_upload_identity_includes_event_source_message(
    client,
    db_session,
    tmp_path,
) -> None:
    first_body = b"first-message-file"
    second_body = b"second-message-file"
    first = _event_payload()
    first["history"][0]["files"][0]["size"] = len(first_body)
    first["history"][0]["files"][0]["sha256"] = hashlib.sha256(first_body).hexdigest()
    second = json.loads(json.dumps(first))
    second["eventId"] = "site-support:741:1301"
    second["eventType"] = "ticket.message_added"
    second["history"].append(
        {
            "messageId": 1301,
            "authorKind": "customer",
            "createdAt": "2026-08-22T12:01:00+03:00",
            "text": "Ещё одно вложение",
            "files": [
                {
                    "fileId": 93287,
                    "name": "second.jpg",
                    "mimeType": "image/jpeg",
                    "size": len(second_body),
                    "sha256": hashlib.sha256(second_body).hexdigest(),
                }
            ],
        }
    )
    settings = _settings(site_service_requests_file_spool_dir=str(tmp_path))

    with _api_dependencies(db_session, settings):
        first_response = _post_event(client, first)
        second_response = _post_event(client, second)
        assert first_response.status_code == 202
        assert first_response.json()["missingFileIds"] == [93287]
        assert second_response.status_code == 202
        assert second_response.json()["missingFileIds"] == [93287]
        uploaded = _put_file(
            client,
            event_id=second["eventId"],
            file_id=93287,
            body=second_body,
            filename="second.jpg",
        )

    assert uploaded.status_code == 200
    rows = db_session.scalars(
        select(SiteServiceRequestFile).order_by(SiteServiceRequestFile.source_message_id)
    ).all()
    assert [(row.source_message_id, row.status) for row in rows] == [
        (1201, "pending"),
        (1301, "staged"),
    ]


def test_event_requests_only_files_from_its_own_source_message(client, db_session) -> None:
    first = _event_payload()
    second = json.loads(json.dumps(first))
    second["eventId"] = "site-support:741:1301"
    second["eventType"] = "ticket.message_added"
    second["history"].append(
        {
            "messageId": 1301,
            "authorKind": "customer",
            "createdAt": "2026-08-22T12:01:00+03:00",
            "text": "Новое вложение",
            "files": [
                {
                    "fileId": 93288,
                    "name": "second.jpg",
                    "mimeType": "image/jpeg",
                    "size": 2048,
                    "sha256": "b" * 64,
                }
            ],
        }
    )

    with _api_dependencies(db_session, _settings()):
        assert _post_event(client, first).json()["missingFileIds"] == [93287]
        response = _post_event(client, second)

    assert response.status_code == 202
    assert response.json()["missingFileIds"] == [93288]


def test_uploaded_file_does_not_regress_on_malformed_transport_retry(
    client,
    db_session,
    tmp_path,
) -> None:
    body = b"expected-file"
    payload = _event_payload()
    payload["history"][0]["files"][0]["size"] = len(body)
    payload["history"][0]["files"][0]["sha256"] = hashlib.sha256(body).hexdigest()
    settings = _settings(site_service_requests_file_spool_dir=str(tmp_path))

    with _api_dependencies(db_session, settings):
        assert _post_event(client, payload).status_code == 202
        file = db_session.scalar(select(SiteServiceRequestFile))
        assert file is not None
        file.status = "uploaded"
        file.bitrix_object_id = "2000"
        db_session.commit()
        retry = _put_file(
            client,
            event_id=payload["eventId"],
            file_id=93287,
            body=b"bad",
            filename="wrong.jpg",
        )

    assert retry.status_code == 200
    assert retry.json()["duplicate"] is True
    db_session.refresh(file)
    assert file.status == "uploaded"
    assert file.last_error_code is None


def test_file_api_rejects_unregistered_unsafe_or_changed_content(
    client,
    db_session,
    tmp_path,
) -> None:
    expected = b"expected-file"
    payload = _event_payload()
    payload["history"][0]["files"][0]["size"] = len(expected)
    payload["history"][0]["files"][0]["sha256"] = hashlib.sha256(expected).hexdigest()
    settings = _settings(site_service_requests_file_spool_dir=str(tmp_path))

    with _api_dependencies(db_session, settings):
        assert _post_event(client, payload).status_code == 202
        missing = _put_file(
            client,
            event_id=payload["eventId"],
            file_id=99999,
            body=expected,
        )
        unsafe = _put_file(
            client,
            event_id=payload["eventId"],
            file_id=93287,
            body=expected,
            filename="../photo.jpg",
        )
        changed = _put_file(
            client,
            event_id=payload["eventId"],
            file_id=93287,
            body=b"changed-file!",
        )

    assert missing.status_code == 404
    assert missing.json() == {"detail": "file_not_registered"}
    assert unsafe.status_code == 422
    assert unsafe.json() == {"detail": "file_name_invalid"}
    assert changed.status_code == 422
    assert changed.json() == {"detail": "file_hash_mismatch"}
    file = db_session.scalar(select(SiteServiceRequestFile))
    assert file is not None and file.status == "failed"
    assert file.last_error_code == "file_hash_mismatch"
    assert file.temporary_path is None
    assert file.case.sync_status == "file_sync_error"
    assert list(tmp_path.rglob("*.bin")) == []


def test_commands_api_leases_decrypted_command_and_releases_it_after_expiry(
    client,
    db_session,
) -> None:
    settings = _settings(
        site_service_requests_outbound_replies_enabled=True,
        site_service_requests_command_lease_seconds=300,
    )
    with _api_dependencies(db_session, settings):
        assert _post_event(client, _event_payload()).status_code == 202
        case = db_session.scalar(select(SiteServiceRequestCase))
        assert case is not None
        command = _create_command(db_session, case_id=case.id)

        first = _get_commands(client)
        second = _get_commands(client)
        command.lease_until = datetime(2020, 1, 1, tzinfo=UTC)
        db_session.commit()
        after_expiry = _get_commands(client)

    assert first.status_code == 200
    assert first.json()["schemaVersion"] == 1
    first_lease_token = first.json()["commands"][0]["leaseToken"]
    second_lease_token = after_expiry.json()["commands"][0]["leaseToken"]
    assert first.json()["commands"] == [
        {
            "commandId": command.id,
            "commandKey": command.command_key,
            "ticketId": 741,
            "replyText": "Проверочный ответ клиенту",
            "leaseUntil": first.json()["commands"][0]["leaseUntil"],
            "leaseToken": first_lease_token,
        }
    ]
    assert second.status_code == 200
    assert second.json()["commands"] == []
    assert after_expiry.status_code == 200
    assert len(after_expiry.json()["commands"]) == 1
    assert second_lease_token != first_lease_token
    db_session.refresh(command)
    assert command.status == "leased"
    assert command.attempts == 2


def test_command_ack_rejects_stale_lease_token_after_rotation(client, db_session) -> None:
    settings = _settings(site_service_requests_outbound_replies_enabled=True)
    with _api_dependencies(db_session, settings):
        assert _post_event(client, _event_payload()).status_code == 202
        case = db_session.scalar(select(SiteServiceRequestCase))
        assert case is not None
        command = _create_command(db_session, case_id=case.id)
        first = _get_commands(client)
        first_token = first.json()["commands"][0]["leaseToken"]
        command.lease_until = datetime(2020, 1, 1, tzinfo=UTC)
        db_session.commit()
        second = _get_commands(client)
        second_token = second.json()["commands"][0]["leaseToken"]
        ack_payload = {
            "schemaVersion": 1,
            "status": "applied",
            "ticketId": 741,
            "messageId": 1301,
            "appliedAt": "2026-08-22T13:00:00+03:00",
        }
        stale = _post_command_ack(
            client,
            command.id,
            {**ack_payload, "leaseToken": first_token},
        )
        applied = _post_command_ack(
            client,
            command.id,
            {**ack_payload, "leaseToken": second_token},
        )

    assert first_token != second_token
    assert stale.status_code == 409
    assert stale.json() == {"detail": "command_lease_conflict"}
    assert applied.status_code == 200


def test_invalid_command_text_is_quarantined_without_blocking_valid_batch(
    client,
    db_session,
) -> None:
    settings = _settings(site_service_requests_outbound_replies_enabled=True)
    with _api_dependencies(db_session, settings):
        assert _post_event(client, _event_payload()).status_code == 202
        case = db_session.scalar(select(SiteServiceRequestCase))
        assert case is not None
        invalid_key = "site-support-reply:741:invalid"
        invalid = SiteServiceRequestCommand(
            case_id=case.id,
            command_key=invalid_key,
            reply_encrypted=SiteServiceRequestCipher(_ENCRYPTION_KEY).encrypt(
                b"\xff",
                event_id=invalid_key,
            ),
            reply_sha256=hashlib.sha256(b"\xff").hexdigest(),
            status="pending",
        )
        db_session.add(invalid)
        db_session.commit()
        valid = _create_command(
            db_session,
            case_id=case.id,
            command_key="site-support-reply:741:valid",
        )
        response = _get_commands(client)

    assert response.status_code == 200
    assert [row["commandId"] for row in response.json()["commands"]] == [valid.id]
    db_session.refresh(invalid)
    assert invalid.status == "failed"
    assert invalid.last_error_code == "command_payload_invalid"
    assert invalid.card_action_cleared_at is not None


def test_wrong_global_command_key_fails_closed_without_discarding_command(
    client,
    db_session,
) -> None:
    with _api_dependencies(db_session, _settings()):
        assert _post_event(client, _event_payload()).status_code == 202
    case = db_session.scalar(select(SiteServiceRequestCase))
    assert case is not None
    command = _create_command(db_session, case_id=case.id)
    wrong_key = base64.urlsafe_b64encode(b"z" * 32).decode("ascii")

    with _api_dependencies(
        db_session,
        _settings(
            site_service_requests_outbound_replies_enabled=True,
            site_service_requests_event_encryption_key=wrong_key,
        ),
    ):
        response = _get_commands(client)

    assert response.status_code == 503
    db_session.refresh(command)
    assert command.status == "pending"
    assert command.last_error_code is None


def test_commands_api_returns_at_most_twenty_and_disabled_mode_does_not_lease(
    client,
    db_session,
) -> None:
    with _api_dependencies(db_session, _settings()):
        assert _post_event(client, _event_payload()).status_code == 202
        case = db_session.scalar(select(SiteServiceRequestCase))
        assert case is not None
        for index in range(21):
            _create_command(
                db_session,
                case_id=case.id,
                command_key=f"site-support-reply:741:{index}",
                reply_text=f"Ответ {index}",
            )
        disabled = _get_commands(client)

    assert disabled.status_code == 200
    assert disabled.json()["commands"] == []
    assert (
        db_session.scalar(
            select(func.count(SiteServiceRequestCommand.id)).where(
                SiteServiceRequestCommand.status == "pending"
            )
        )
        == 21
    )

    with _api_dependencies(
        db_session,
        _settings(site_service_requests_outbound_replies_enabled=True),
    ):
        first = _get_commands(client)
        second = _get_commands(client)

    assert len(first.json()["commands"]) == 20
    assert len(second.json()["commands"]) == 1


def test_command_ack_is_idempotent_and_does_not_complete_sla_without_readback(
    client,
    db_session,
) -> None:
    settings = _settings(site_service_requests_outbound_replies_enabled=True)
    with _api_dependencies(db_session, settings):
        assert _post_event(client, _event_payload()).status_code == 202
        case = db_session.scalar(select(SiteServiceRequestCase))
        assert case is not None
        command = _create_command(db_session, case_id=case.id)
        leased = _get_commands(client)
        assert leased.status_code == 200
        lease_token = leased.json()["commands"][0]["leaseToken"]
        ack_payload = {
            "schemaVersion": 1,
            "leaseToken": lease_token,
            "status": "applied",
            "ticketId": 741,
            "messageId": 1301,
            "appliedAt": "2026-08-22T13:00:00+03:00",
        }
        applied = _post_command_ack(client, command.id, ack_payload)
        duplicate = _post_command_ack(client, command.id, ack_payload)
        conflicting_payload = {**ack_payload, "messageId": 1302}
        conflict = _post_command_ack(client, command.id, conflicting_payload)

    assert applied.status_code == 200
    assert applied.json() == {
        "commandId": command.id,
        "status": "applied",
        "duplicate": False,
    }
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True
    assert conflict.status_code == 409
    assert conflict.json() == {"detail": "command_ack_conflict"}
    db_session.refresh(command)
    db_session.refresh(case)
    assert command.status == "applied"
    assert command.source_message_id == 1301
    assert command.lease_until is None
    assert case.latest_outbound_message_id == 1301
    assert case.first_response_at is None


def test_command_ack_locks_case_before_command(client, db_session) -> None:
    with _api_dependencies(db_session, _settings()):
        assert _post_event(client, _event_payload()).status_code == 202
    case = db_session.scalar(select(SiteServiceRequestCase))
    assert case is not None
    command = _create_command(db_session, case_id=case.id)
    command.status = "leased"
    command.lease_token = "l" * 32
    db_session.commit()

    class RecordingSession:
        def __init__(self) -> None:
            self.statements = []
            self.results = iter((case.id, case, command, command.id))
            self.flushed = False

        def scalar(self, statement):
            self.statements.append(statement)
            return next(self.results)

        def flush(self) -> None:
            self.flushed = True

    recording_session = RecordingSession()
    result = acknowledge_site_service_request_command(
        recording_session,  # type: ignore[arg-type]
        command_id=command.id,
        payload=SiteServiceRequestCommandAckPayload.model_validate(
            {
                "schemaVersion": 1,
                "leaseToken": command.lease_token,
                "status": "applied",
                "ticketId": case.source_ticket_id,
                "messageId": 1301,
                "appliedAt": "2026-08-22T13:00:00+03:00",
            }
        ),
    )

    entities = [
        statement.column_descriptions[0]["entity"] for statement in recording_session.statements
    ]
    assert entities == [
        SiteServiceRequestCommand,
        SiteServiceRequestCase,
        SiteServiceRequestCommand,
        SiteServiceRequestCommand,
    ]
    assert recording_session.statements[0]._for_update_arg is None
    assert recording_session.statements[1]._for_update_arg is not None
    assert recording_session.statements[2]._for_update_arg is not None
    assert recording_session.statements[3]._for_update_arg is None
    assert recording_session.flushed is True
    assert result.status == "applied"
    assert case.latest_outbound_message_id == 1301


def test_command_failed_ack_has_allowlisted_error_and_is_idempotent(
    client,
    db_session,
) -> None:
    settings = _settings(site_service_requests_outbound_replies_enabled=True)
    with _api_dependencies(db_session, settings):
        assert _post_event(client, _event_payload()).status_code == 202
        case = db_session.scalar(select(SiteServiceRequestCase))
        assert case is not None
        command = _create_command(db_session, case_id=case.id)
        leased = _get_commands(client)
        assert leased.status_code == 200
        lease_token = leased.json()["commands"][0]["leaseToken"]
        ack_payload = {
            "schemaVersion": 1,
            "leaseToken": lease_token,
            "status": "failed",
            "errorCode": "message_write_failed",
        }
        failed = _post_command_ack(client, command.id, ack_payload)
        duplicate = _post_command_ack(client, command.id, ack_payload)
        applied_after_failure = _post_command_ack(
            client,
            command.id,
            {
                "schemaVersion": 1,
                "leaseToken": lease_token,
                "status": "applied",
                "ticketId": 741,
                "messageId": 1301,
                "appliedAt": "2026-08-22T13:00:00+03:00",
            },
        )

    assert failed.status_code == 200
    assert failed.json() == {
        "commandId": command.id,
        "status": "failed",
        "duplicate": False,
    }
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True
    assert applied_after_failure.status_code == 409
    assert applied_after_failure.json() == {"detail": "command_ack_conflict"}
    db_session.refresh(command)
    assert command.status == "failed"
    assert command.last_error_code == "message_write_failed"


def test_failed_outbound_health_stays_degraded_until_replacement_is_applied(
    client,
    db_session,
) -> None:
    settings = _settings(site_service_requests_outbound_replies_enabled=True)
    with _api_dependencies(db_session, settings):
        assert _post_event(client, _event_payload()).status_code == 202
        case = db_session.scalar(select(SiteServiceRequestCase))
        assert case is not None
        failed_command = _create_command(db_session, case_id=case.id)
        failed_lease = _get_commands(client).json()["commands"][0]
        assert _post_command_ack(
            client,
            failed_command.id,
            {
                "schemaVersion": 1,
                "leaseToken": failed_lease["leaseToken"],
                "status": "failed",
                "errorCode": "message_write_failed",
            },
        ).status_code == 200
        replacement = _create_command(
            db_session,
            case_id=case.id,
            command_key="site-support-reply:741:replacement",
            reply_text="Исправленный ответ",
        )
        pending_health = client.get(
            _HEALTH_PATH,
            headers=_signed_headers(method="GET", path=_HEALTH_PATH, body=b""),
        )
        replacement_lease = _get_commands(client).json()["commands"][0]
        assert replacement_lease["commandId"] == replacement.id
        assert _post_command_ack(
            client,
            replacement.id,
            {
                "schemaVersion": 1,
                "leaseToken": replacement_lease["leaseToken"],
                "status": "applied",
                "ticketId": 741,
                "messageId": 1302,
                "appliedAt": "2026-08-22T13:05:00+03:00",
            },
        ).status_code == 200
        recovered_health = client.get(
            _HEALTH_PATH,
            headers=_signed_headers(method="GET", path=_HEALTH_PATH, body=b""),
        )

    assert pending_health.json()["status"] == "degraded"
    assert pending_health.json()["outboundFailures"] == 1
    assert recovered_health.json()["status"] == "healthy"
    assert recovered_health.json()["outboundFailures"] == 0


def test_health_contains_only_safe_technical_aggregates(client, db_session) -> None:
    payload = _event_payload()
    with _api_dependencies(db_session, _settings()):
        assert _post_event(client, payload).status_code == 202
        body = b""
        health = client.get(
            _HEALTH_PATH,
            headers=_signed_headers(method="GET", path=_HEALTH_PATH, body=body),
        )

    assert health.status_code == 200
    result = health.json()
    assert result["status"] == "healthy"
    assert result["alertCodes"] == []
    assert result["pendingEvents"] == 1
    assert result["failedEvents"] == 0
    assert result["pendingCommands"] == 0
    assert result["unlinkedCases"] == 1
    assert result["assignmentFailures"] == 0
    assert result["outboundFailures"] == 0
    assert result["pendingEscalationDeliveries"] == 0
    assert result["lastSuccessfulExchangeAt"] is None
    assert result["ingestEnabled"] is True
    rendered = json.dumps(result, ensure_ascii=False)
    for sensitive_value in (
        payload["ticket"]["phone"],
        payload["ticket"]["email"],
        payload["history"][0]["text"],
        payload["history"][0]["files"][0]["name"],
    ):
        assert sensitive_value not in rendered


def test_health_degrades_with_safe_lag_and_dead_letter_alerts(client, db_session) -> None:
    with _api_dependencies(db_session, _settings()):
        assert _post_event(client, _event_payload()).status_code == 202
        second_payload = _event_payload()
        second_payload["eventId"] = "site-support:742:1202"
        second_payload["ticket"]["id"] = 742
        second_payload["history"][0]["messageId"] = 1202
        second_payload["history"][0]["files"][0]["fileId"] = 93288
        assert _post_event(client, second_payload).status_code == 202

        events = db_session.scalars(
            select(SiteServiceRequestEvent).order_by(SiteServiceRequestEvent.id)
        ).all()
        assert len(events) == 2
        events[0].status = "needs_attention"
        events[1].created_at = datetime.now(UTC) - timedelta(minutes=6)
        db_session.commit()

        health = client.get(
            _HEALTH_PATH,
            headers=_signed_headers(method="GET", path=_HEALTH_PATH, body=b""),
        )

    assert health.status_code == 200
    result = health.json()
    assert result["status"] == "degraded"
    assert result["alertCodes"] == ["dead_letter", "event_lag"]
    assert result["failedEvents"] == 1


@pytest.mark.parametrize(
    ("failure_kind", "alert_code", "counter_name"),
    [
        ("assignment", "assignment_failure", "assignmentFailures"),
        ("outbound", "outbound_failure", "outboundFailures"),
        (
            "escalation",
            "escalation_delivery_pending",
            "pendingEscalationDeliveries",
        ),
    ],
)
def test_health_degrades_for_worker_failure_and_recovers_after_retry(
    client,
    db_session,
    failure_kind: str,
    alert_code: str,
    counter_name: str,
) -> None:
    settings = _settings(site_service_requests_escalation_user_id=1003)
    with _api_dependencies(db_session, settings):
        assert _post_event(client, _event_payload()).status_code == 202
        case = db_session.scalar(select(SiteServiceRequestCase))
        assert case is not None
        if failure_kind == "assignment":
            case.assignment_last_error_code = "PRIVATE-ASSIGNMENT-DETAIL"
        elif failure_kind == "outbound":
            case.outbound_last_error_code = "PRIVATE-OUTBOUND-DETAIL"
        else:
            case.escalated_at = datetime.now(UTC)
        db_session.commit()

        degraded = client.get(
            _HEALTH_PATH,
            headers=_signed_headers(method="GET", path=_HEALTH_PATH, body=b""),
        )

        if failure_kind == "assignment":
            case.assignment_last_error_code = None
        elif failure_kind == "outbound":
            case.outbound_last_error_code = None
        else:
            delivered_at = datetime.now(UTC)
            case.escalation_timeline_delivered_at = delivered_at
            case.escalation_notification_delivered_at = delivered_at
        db_session.commit()

        recovered = client.get(
            _HEALTH_PATH,
            headers=_signed_headers(method="GET", path=_HEALTH_PATH, body=b""),
        )

    assert degraded.status_code == 200
    degraded_result = degraded.json()
    assert degraded_result["status"] == "degraded"
    assert degraded_result["alertCodes"] == [alert_code]
    assert degraded_result[counter_name] == 1
    assert "PRIVATE-" not in degraded.text

    assert recovered.status_code == 200
    recovered_result = recovered.json()
    assert recovered_result["status"] == "healthy"
    assert recovered_result["alertCodes"] == []
    assert recovered_result["assignmentFailures"] == 0
    assert recovered_result["outboundFailures"] == 0
    assert recovered_result["pendingEscalationDeliveries"] == 0


def test_validation_error_does_not_echo_customer_payload(client, db_session) -> None:
    payload = _event_payload()
    payload["ticket"].pop("requestType")
    payload["history"][0]["text"] = "PRIVATE-CUSTOMER-TEXT"
    body = _json_body(payload)
    with _api_dependencies(db_session, _settings()):
        response = client.post(
            _EVENT_PATH,
            content=body,
            headers=_signed_headers(method="POST", path=_EVENT_PATH, body=body),
        )

    assert response.status_code == 422
    assert response.json() == {"detail": "invalid site service request"}
    assert "PRIVATE-CUSTOMER-TEXT" not in response.text
