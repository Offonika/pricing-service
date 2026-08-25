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
    SiteServiceRequestSource,
)
from app.services.site_service_requests import SiteServiceRequestCipher
from app.services.site_service_requests_auth import (
    content_sha256,
    sign_site_request,
)

_PATH = "/api/internal/site-service-requests/email-events"
_SECRET = "test-only-service-email-secret"
_ENCRYPTION_KEY = base64.urlsafe_b64encode(b"e" * 32).decode("ascii")


def _settings(**overrides) -> Settings:
    values = {
        "site_service_requests_email_ingest_enabled": True,
        "site_service_requests_hmac_secret": _SECRET,
        "site_service_requests_event_encryption_key": _ENCRYPTION_KEY,
    }
    values.update(overrides)
    return Settings(**values)


def _payload(
    *,
    mailbox: str = "shop",
    message_id: int = 88001,
    activity_id: int = 99001,
    thread_id: int = 77001,
    existing_item_id: int | None = None,
) -> dict:
    payload = {
        "schemaVersion": 1,
        "eventId": f"bitrix-mail:{mailbox}:{message_id}",
        "eventType": "email.received",
        "occurredAt": "2026-08-25T12:00:00+03:00",
        "mailbox": mailbox,
        "messageId": message_id,
        "activityId": activity_id,
        "threadId": thread_id,
        "requestType": "warranty",
        "orderNumber": "241887",
        "crmContactId": 501,
        "crmDealId": 601,
    }
    if existing_item_id is not None:
        payload["existingServiceItemId"] = existing_item_id
    return payload


def _body(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()


def _headers(body: bytes) -> dict[str, str]:
    timestamp = int(datetime.now(UTC).timestamp())
    nonce = str(uuid4())
    digest = content_sha256(body)
    return {
        "Content-Type": "application/json",
        "X-MM-Site-Timestamp": str(timestamp),
        "X-MM-Site-Nonce": nonce,
        "X-MM-Site-Content-SHA256": digest,
        "X-MM-Site-Signature": sign_site_request(
            secret=_SECRET,
            timestamp=timestamp,
            nonce=nonce,
            method="POST",
            path=_PATH,
            body_sha256=digest,
        ),
    }


@contextmanager
def _dependencies(db_session, settings: Settings):
    def override_db():
        yield db_session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_site_service_request_settings] = lambda: settings
    try:
        yield
    finally:
        app.dependency_overrides.clear()


def _post(client, payload: dict):
    body = _body(payload)
    return client.post(_PATH, content=body, headers=_headers(body))


def test_email_endpoint_is_separately_feature_gated(client, db_session) -> None:
    with _dependencies(
        db_session,
        _settings(site_service_requests_email_ingest_enabled=False),
    ):
        response = _post(client, _payload())

    assert response.status_code == 503
    assert response.json() == {"detail": "email_ingest_disabled"}
    assert db_session.scalar(select(func.count(SiteServiceRequestEvent.id))) == 0


def test_email_endpoint_persists_only_technical_encrypted_payload(client, db_session) -> None:
    payload = _payload(mailbox="info")
    raw_body = _body(payload)
    with _dependencies(db_session, _settings()):
        response = _post(client, payload)

    assert response.status_code == 202
    assert response.json() == {
        "eventId": "bitrix-mail:info:88001",
        "status": "accepted",
        "duplicate": False,
        "missingFileIds": [],
    }
    case = db_session.scalar(select(SiteServiceRequestCase))
    event = db_session.scalar(select(SiteServiceRequestEvent))
    source = db_session.scalar(select(SiteServiceRequestSource))
    assert case is not None and case.source_ticket_id < 0
    assert case.source_kind == "bitrix_mail"
    assert case.source_key == "bitrix-mail:info:77001"
    assert source is not None and source.case_id == case.id
    assert event is not None and event.source_activity_id == 99001
    assert raw_body not in (event.payload_encrypted or b"")
    assert (
        SiteServiceRequestCipher(_ENCRYPTION_KEY).decrypt(
            event.payload_encrypted or b"",
            event_id=event.event_id,
        )
        == raw_body
    )
    assert b"@" not in raw_body


def test_email_event_repeated_ten_times_creates_one_case(client, db_session) -> None:
    with _dependencies(db_session, _settings()):
        responses = [_post(client, _payload()) for _ in range(10)]

    assert responses[0].json()["duplicate"] is False
    assert all(response.status_code == 202 for response in responses)
    assert all(response.json()["duplicate"] is True for response in responses[1:])
    assert db_session.scalar(select(func.count(SiteServiceRequestCase.id))) == 1
    assert db_session.scalar(select(func.count(SiteServiceRequestEvent.id))) == 1
    assert db_session.scalar(select(func.count(SiteServiceRequestSource.id))) == 1


def test_email_event_conflict_is_rejected(client, db_session) -> None:
    original = _payload()
    changed = {**original, "crmDealId": 602}
    with _dependencies(db_session, _settings()):
        assert _post(client, original).status_code == 202
        conflict = _post(client, changed)

    assert conflict.status_code == 409
    assert conflict.json() == {"detail": "event_payload_conflict"}


def test_same_thread_updates_one_case_but_different_thread_does_not_merge(
    client,
    db_session,
) -> None:
    with _dependencies(db_session, _settings()):
        assert _post(client, _payload()).status_code == 202
        assert (
            _post(
                client,
                _payload(message_id=88002, activity_id=99002),
            ).status_code
            == 202
        )
        assert (
            _post(
                client,
                _payload(message_id=88003, activity_id=99003, thread_id=77002),
            ).status_code
            == 202
        )

    assert db_session.scalar(select(func.count(SiteServiceRequestCase.id))) == 2
    assert db_session.scalar(select(func.count(SiteServiceRequestEvent.id))) == 3
    assert db_session.scalar(select(func.count(SiteServiceRequestSource.id))) == 2


def test_technical_existing_item_link_reuses_site_case_without_changing_source(
    client,
    db_session,
) -> None:
    site_case = SiteServiceRequestCase(
        source_ticket_id=741,
        source_kind="site_ticket",
        source_key="site-support-ticket:741",
        bitrix_item_id=4321,
    )
    db_session.add(site_case)
    db_session.commit()

    with _dependencies(db_session, _settings()):
        response = _post(client, _payload(existing_item_id=4321))

    assert response.status_code == 202
    db_session.refresh(site_case)
    assert site_case.source_kind == "site_ticket"
    assert site_case.source_key == "site-support-ticket:741"
    source = db_session.scalar(select(SiteServiceRequestSource))
    assert source is not None and source.case_id == site_case.id
    assert source.source_key == "bitrix-mail:shop:77001"
