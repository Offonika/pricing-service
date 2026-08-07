from __future__ import annotations

import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.customer_settlement import CustomerSettlementAssertionJti
from app.services import customer_settlement_auth as auth
from app.services.customer_settlement_auth import (
    CustomerSettlementAuthConfigError,
    CustomerSettlementAuthError,
    create_customer_settlement_assertion,
    verify_and_consume_customer_settlement_assertion,
)


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "customer_settlements_assertion_issuer": "master-mobile.ru",
        "customer_settlements_assertion_audience": ("pricing-service:customer-settlements"),
        "customer_settlements_assertion_active_kid": "active-1",
        "customer_settlements_assertion_active_secret": "synthetic-active-secret",
        "customer_settlements_assertion_previous_kid": None,
        "customer_settlements_assertion_previous_secret": None,
        "customer_settlements_assertion_ttl_seconds": 60,
        "customer_settlements_assertion_clock_skew_seconds": 30,
        "customer_settlements_allowed_source_ips": ["127.0.0.1/32", "10.0.0.0/24"],
    }
    values.update(overrides)
    return Settings(**values)


def _rewrite_token(
    token: str,
    *,
    settings: Settings,
    header_changes: dict[str, object] | None = None,
    payload_changes: dict[str, object] | None = None,
) -> str:
    header_raw, payload_raw, _ = token.split(".")
    header = json.loads(auth._b64_decode(header_raw))
    payload = json.loads(auth._b64_decode(payload_raw))
    header.update(header_changes or {})
    payload.update(payload_changes or {})
    new_header = auth._b64_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    new_payload = auth._b64_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{new_header}.{new_payload}"
    key = auth._keys(settings)[str(header["kid"])]
    return f"{signing_input}.{auth._sign(signing_input, key)}"


def test_assertion_roundtrip_consumes_only_hashed_jti(db_session: Session) -> None:
    settings = _settings()
    token, expires_at = create_customer_settlement_assertion(
        site_user_id="123",
        settings=settings,
        now=1_000,
        jti="synthetic_jti_123456789",
    )

    identity = verify_and_consume_customer_settlement_assertion(
        db_session,
        token=token,
        source_ip="127.0.0.1",
        settings=settings,
        now=1_001,
    )

    assert identity.site_user_id == "123"
    assert int(identity.expires_at.timestamp()) == expires_at
    stored = db_session.scalar(select(CustomerSettlementAssertionJti))
    assert stored is not None
    assert stored.jti_hash != "synthetic_jti_123456789"
    assert len(stored.jti_hash) == 64

    with pytest.raises(CustomerSettlementAuthError) as replay:
        verify_and_consume_customer_settlement_assertion(
            db_session,
            token=token,
            source_ip="127.0.0.1",
            settings=settings,
            now=1_002,
        )
    assert replay.value.code == "replay"


def test_documented_assertion_contract_vector_is_stable() -> None:
    settings = _settings(
        customer_settlements_assertion_active_kid="settlements-test-1",
        customer_settlements_assertion_active_secret="synthetic-contract-secret-v1",
    )
    token, expires_at = create_customer_settlement_assertion(
        site_user_id="12345",
        settings=settings,
        now=1_785_301_200,
        jti="contract_vector_20260729",
    )

    assert expires_at == 1_785_301_260
    assert token == (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6Ik1NLUNVU1RPTUVSLVNFVFRMRU1FTlRTIiwia2lk"
        "Ijoic2V0dGxlbWVudHMtdGVzdC0xIn0."
        "eyJpc3MiOiJtYXN0ZXItbW9iaWxlLnJ1IiwiYXVkIjoicHJpY2luZy1zZXJ2aWNlOmN1"
        "c3RvbWVyLXNldHRsZW1lbnRzIiwic3ViIjoiMTIzNDUiLCJzaXRlX3VzZXJfaWQiOiIx"
        "MjM0NSIsImlhdCI6MTc4NTMwMTIwMCwibmJmIjoxNzg1MzAxMjAwLCJleHAiOjE3ODUz"
        "MDEyNjAsImp0aSI6ImNvbnRyYWN0X3ZlY3Rvcl8yMDI2MDcyOSJ9."
        "GhCE-qIikJ2Im0xZimMcpJEf3PZALN1yGNkoHxEycwk"
    )


@pytest.mark.parametrize(
    ("create_overrides", "verify_overrides"),
    [
        ({"customer_settlements_assertion_issuer": "wrong.example"}, {}),
        (
            {"customer_settlements_assertion_audience": "wrong-audience"},
            {},
        ),
    ],
)
def test_assertion_rejects_wrong_issuer_or_audience(
    db_session: Session,
    create_overrides: dict[str, object],
    verify_overrides: dict[str, object],
) -> None:
    token, _ = create_customer_settlement_assertion(
        site_user_id="123",
        settings=_settings(**create_overrides),
        now=1_000,
        jti="synthetic_jti_123456789",
    )
    with pytest.raises(CustomerSettlementAuthError):
        verify_and_consume_customer_settlement_assertion(
            db_session,
            token=token,
            source_ip="127.0.0.1",
            settings=_settings(**verify_overrides),
            now=1_001,
        )


def test_assertion_rejects_wrong_alg_kid_and_malformed_base64(db_session: Session) -> None:
    settings = _settings()
    token, _ = create_customer_settlement_assertion(
        site_user_id="123",
        settings=settings,
        now=1_000,
        jti="synthetic_jti_123456789",
    )
    invalid_tokens = (
        _rewrite_token(token, settings=settings, header_changes={"alg": "none"}),
        _rewrite_token(token, settings=settings, header_changes={"kid": "active-1"})[:-1] + "x",
        "🧨.invalid.value",
    )
    for invalid_token in invalid_tokens:
        with pytest.raises(CustomerSettlementAuthError):
            verify_and_consume_customer_settlement_assertion(
                db_session,
                token=invalid_token,
                source_ip="127.0.0.1",
                settings=settings,
                now=1_001,
            )


def test_assertion_rejects_expired_future_and_invalid_not_before(
    db_session: Session,
) -> None:
    settings = _settings()
    token, _ = create_customer_settlement_assertion(
        site_user_id="123",
        settings=settings,
        now=1_000,
        jti="synthetic_jti_123456789",
    )
    with pytest.raises(CustomerSettlementAuthError) as expired:
        verify_and_consume_customer_settlement_assertion(
            db_session,
            token=token,
            source_ip="127.0.0.1",
            settings=settings,
            now=1_091,
        )
    assert expired.value.code == "expired"

    future_token, _ = create_customer_settlement_assertion(
        site_user_id="123",
        settings=settings,
        now=1_100,
        jti="synthetic_future_12345678",
    )
    with pytest.raises(CustomerSettlementAuthError) as future:
        verify_and_consume_customer_settlement_assertion(
            db_session,
            token=future_token,
            source_ip="127.0.0.1",
            settings=settings,
            now=1_000,
        )
    assert future.value.code == "future"

    invalid_nbf = _rewrite_token(
        token,
        settings=settings,
        payload_changes={"nbf": 999},
    )
    with pytest.raises(CustomerSettlementAuthError):
        verify_and_consume_customer_settlement_assertion(
            db_session,
            token=invalid_nbf,
            source_ip="127.0.0.1",
            settings=settings,
            now=1_001,
        )


def test_assertion_accepts_previous_rotation_key(db_session: Session) -> None:
    old_settings = _settings(
        customer_settlements_assertion_active_kid="old-key",
        customer_settlements_assertion_active_secret="old-secret",
    )
    token, _ = create_customer_settlement_assertion(
        site_user_id="123",
        settings=old_settings,
        now=1_000,
        jti="synthetic_oldkey_123456",
    )
    rotated_settings = _settings(
        customer_settlements_assertion_active_kid="new-key",
        customer_settlements_assertion_active_secret="new-secret",
        customer_settlements_assertion_previous_kid="old-key",
        customer_settlements_assertion_previous_secret="old-secret",
    )

    identity = verify_and_consume_customer_settlement_assertion(
        db_session,
        token=token,
        source_ip="10.0.0.5",
        settings=rotated_settings,
        now=1_001,
    )
    assert identity.site_user_id == "123"


def test_assertion_rejects_unlisted_ip_and_duplicate_key_ids(db_session: Session) -> None:
    settings = _settings()
    token, _ = create_customer_settlement_assertion(
        site_user_id="123",
        settings=settings,
        now=1_000,
        jti="synthetic_jti_123456789",
    )
    with pytest.raises(CustomerSettlementAuthError):
        verify_and_consume_customer_settlement_assertion(
            db_session,
            token=token,
            source_ip="192.0.2.1",
            settings=settings,
            now=1_001,
        )

    duplicate = _settings(
        customer_settlements_assertion_previous_kid="active-1",
        customer_settlements_assertion_previous_secret="previous-secret",
    )
    with pytest.raises(CustomerSettlementAuthConfigError):
        create_customer_settlement_assertion(
            site_user_id="123",
            settings=duplicate,
            now=1_000,
            jti="synthetic_jti_123456789",
        )
