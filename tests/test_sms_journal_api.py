from __future__ import annotations

import base64
from collections.abc import Generator
from types import SimpleNamespace

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

import app.api.dependencies as dependencies_module
import app.api.sms_journal as sms_api_module
from app.api.dependencies import get_db
from app.main import app
from app.models import Base
from app.models.sms_journal import SmsJournalAttempt
from app.services.sms_journal import calculate_sms_segments


def _settings() -> SimpleNamespace:
    key = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii")
    return SimpleNamespace(
        sms_journal_internal_api_token="test-sms-token",
        sms_journal_encryption_key=key,
        sms_journal_phone_hash_key="phone-hash-key-for-tests-at-least-32-bytes",
    )


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "source_system": "ut10.3",
        "source_entity_type": "customer_order",
        "source_entity_id": "ORDER-2662",
        "event_type": "order_status",
        "recipient_phone": "+7 999 123-45-67",
        "message_text": "Заказ ORDER-2662 принят",
        "provider": "megafon",
    }
    payload.update(overrides)
    return payload


def _client(tmp_path, monkeypatch) -> tuple[TestClient, sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'sms-journal.db'}")
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(engine)

    def override_db() -> Generator[Session, None, None]:
        with factory() as session:
            yield session

    settings = _settings()
    monkeypatch.setattr(sms_api_module, "get_settings", lambda: settings)
    monkeypatch.setattr(dependencies_module, "get_settings", lambda: settings)
    app.dependency_overrides[get_db] = override_db
    return TestClient(app), factory


def _headers(key: str = "create-order-2662") -> dict[str, str]:
    return {
        "Authorization": "Bearer test-sms-token",
        "Idempotency-Key": key,
    }


def test_segment_boundaries_and_gsm_extension_characters() -> None:
    assert calculate_sms_segments("A" * 160) == ("GSM-7", 1)
    assert calculate_sms_segments("A" * 161) == ("GSM-7", 2)
    assert calculate_sms_segments("^" * 80) == ("GSM-7", 1)
    assert calculate_sms_segments("^" * 81) == ("GSM-7", 2)
    assert calculate_sms_segments("Я" * 70) == ("UCS-2", 1)
    assert calculate_sms_segments("Я" * 71) == ("UCS-2", 2)
    assert calculate_sms_segments("Я" * 134) == ("UCS-2", 2)
    assert calculate_sms_segments("Я" * 135) == ("UCS-2", 3)


def test_attempt_lifecycle_is_encrypted_masked_and_idempotent(tmp_path, monkeypatch) -> None:
    client, factory = _client(tmp_path, monkeypatch)
    try:
        created = client.post(
            "/api/internal/sms-journal/attempts",
            headers=_headers(),
            json=_payload(),
        )
        assert created.status_code == 201
        body = created.json()
        assert body["recipient_phone_masked"] == "+***4567"
        assert "recipient_phone" not in body
        assert "message_text" not in body
        assert body["send_status"] == "pending"
        assert body["encoding"] == "UCS-2"

        replay = client.post(
            "/api/internal/sms-journal/attempts",
            headers=_headers(),
            json=_payload(),
        )
        assert replay.status_code == 201
        assert replay.json()["event_id"] == body["event_id"]
        assert replay.json()["idempotency_replayed"] is True

        conflict = client.post(
            "/api/internal/sms-journal/attempts",
            headers=_headers(),
            json=_payload(source_entity_id="OTHER"),
        )
        assert conflict.status_code == 409

        send_result = client.post(
            f"/api/internal/sms-journal/attempts/{body['event_id']}/send-result",
            headers=_headers("send-result-order-2662"),
            json={
                "send_status": "accepted",
                "provider_message_id": "provider-message-1",
                "billed_segments": 1,
                "unit_price": "7.96",
                "total_cost": "7.96",
                "reconciliation_period": "2026-08",
                "provider_error_detail": "diagnostic for +7 999 123-45-67",
            },
        )
        assert send_result.status_code == 200
        assert send_result.json()["provider_message_id"] == "provider-message-1"

        delivered = client.post(
            f"/api/internal/sms-journal/attempts/{body['event_id']}/delivery",
            headers=_headers("delivery-order-2662"),
            json={"delivery_status": "delivered"},
        )
        assert delivered.status_code == 200
        assert delivered.json()["delivery_status"] == "delivered"

        readback = client.get(
            f"/api/internal/sms-journal/attempts/{body['event_id']}",
            headers={"Authorization": "Bearer test-sms-token"},
        )
        assert readback.status_code == 200
        assert "message_text" not in readback.json()
        assert "provider_error_detail" not in readback.json()

        with factory() as session:
            row = session.scalar(select(SmsJournalAttempt))
            assert row is not None
            assert "+7 999 123-45-67" not in row.recipient_phone_encrypted
            assert "Заказ ORDER-2662 принят" not in row.message_text_encrypted
            assert row.provider_error_detail_encrypted is not None
            assert "+7 999 123-45-67" not in row.provider_error_detail_encrypted
    finally:
        app.dependency_overrides.clear()


def test_otp_is_redacted_before_encryption_and_missing_redaction_is_rejected(
    tmp_path, monkeypatch
) -> None:
    client, factory = _client(tmp_path, monkeypatch)
    try:
        missing = client.post(
            "/api/internal/sms-journal/attempts",
            headers=_headers("missing-redaction"),
            json=_payload(message_text="Код: 123456", secret_kind="otp"),
        )
        assert missing.status_code == 422
        assert "123456" not in missing.text

        created = client.post(
            "/api/internal/sms-journal/attempts",
            headers=_headers("redacted-otp"),
            json=_payload(
                message_text="Код: 123456",
                secret_kind="otp",
                redaction_values=["123456"],
            ),
        )
        assert created.status_code == 201
        assert created.json()["contains_redacted_secret"] is True

        with factory() as session:
            row = session.scalar(select(SmsJournalAttempt))
            assert row is not None
            raw = base64.urlsafe_b64decode(row.message_text_encrypted.encode("ascii"))
            key = base64.urlsafe_b64decode(_settings().sms_journal_encryption_key)
            decrypted = AESGCM(key).decrypt(raw[:12], raw[12:], None).decode("utf-8")
            assert decrypted == "Код: [REDACTED]"
            assert "123456" not in row.message_text_encrypted
    finally:
        app.dependency_overrides.clear()


def test_authentication_and_invalid_delivery_transition(tmp_path, monkeypatch) -> None:
    client, _factory = _client(tmp_path, monkeypatch)
    try:
        unauthorized = client.post(
            "/api/internal/sms-journal/attempts",
            headers={"Idempotency-Key": "unauthorized-create"},
            json=_payload(),
        )
        assert unauthorized.status_code == 401

        created = client.post(
            "/api/internal/sms-journal/attempts",
            headers=_headers("create-for-invalid-delivery"),
            json=_payload(),
        )
        event_id = created.json()["event_id"]
        invalid = client.post(
            f"/api/internal/sms-journal/attempts/{event_id}/delivery",
            headers=_headers("invalid-delivery"),
            json={
                "delivery_status": "undelivered",
                "delivered_at": "2026-08-10T12:00:00Z",
            },
        )
        assert invalid.status_code == 409
    finally:
        app.dependency_overrides.clear()
