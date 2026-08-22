from __future__ import annotations

import base64
import json
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import func, select

from app.api.dependencies import get_db, get_site_service_request_settings
from app.core.config import Settings
from app.main import app
from app.models.site_service_requests import (
    SiteServiceRequestCase,
    SiteServiceRequestEvent,
    SiteServiceRequestFile,
    SiteServiceRequestNonce,
)
from app.services.site_service_requests import SiteServiceRequestCipher
from app.services.site_service_requests_auth import content_sha256, sign_site_request

_SECRET = "test-only-site-service-request-hmac-secret"
_ENCRYPTION_KEY = base64.urlsafe_b64encode(b"k" * 32).decode("ascii")
_EVENT_PATH = "/api/internal/site-service-requests/events"
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
) -> dict[str, str]:
    timestamp = int(datetime.now(UTC).timestamp())
    nonce_value = nonce or str(uuid4())
    digest = content_sha256(body)
    return {
        "Content-Type": "application/json",
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


def test_event_api_rejects_payload_conflict_without_overwriting_event(
    client,
    db_session,
) -> None:
    original = _event_payload()
    changed = _event_payload()
    changed["ticket"]["title"] = "Другой текст"
    with _api_dependencies(db_session, _settings()):
        assert _post_event(client, original).status_code == 202
        conflict = _post_event(client, changed)

    assert conflict.status_code == 409
    assert conflict.json() == {"detail": "event_payload_conflict"}
    assert db_session.scalar(select(func.count(SiteServiceRequestEvent.id))) == 1
    event = db_session.scalar(select(SiteServiceRequestEvent))
    assert event is not None
    assert event.payload_sha256 == content_sha256(_json_body(original))


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


def test_event_api_rejects_oversized_file_before_persistence(client, db_session) -> None:
    payload = _event_payload()
    payload["history"][0]["files"][0]["size"] = 10 * 1024 * 1024 + 1
    with _api_dependencies(db_session, _settings()):
        response = _post_event(client, payload)

    assert response.status_code == 409
    assert response.json() == {"detail": "file_too_large"}
    assert db_session.scalar(select(func.count(SiteServiceRequestEvent.id))) == 0


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
    assert result["pendingEvents"] == 1
    assert result["failedEvents"] == 0
    assert result["pendingCommands"] == 0
    assert result["unlinkedCases"] == 1
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
