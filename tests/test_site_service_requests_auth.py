from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.base import Base
from app.models.site_service_requests import SiteServiceRequestNonce
from app.services.site_service_requests_auth import (
    SiteServiceRequestAuthError,
    content_sha256,
    sign_site_request,
    verify_site_request,
)

_SECRET = "test-only-site-service-secret"
_NOW = datetime(2026, 8, 22, 9, 0, tzinfo=UTC)
_TIMESTAMP = int(_NOW.timestamp())
_NONCE = "11111111-1111-4111-8111-111111111111"
_PATH = "/api/internal/site-service-requests/events"
_BODY = b'{"schemaVersion":1,"eventId":"site-support:741:1201"}'


def _settings(**overrides) -> Settings:
    return Settings(
        site_service_requests_hmac_secret=_SECRET,
        site_service_requests_timestamp_tolerance_seconds=300,
        site_service_requests_nonce_ttl_seconds=600,
        **overrides,
    )


def _headers(*, body: bytes = _BODY, timestamp: int = _TIMESTAMP, nonce: str = _NONCE):
    digest = content_sha256(body)
    return {
        "timestamp_header": str(timestamp),
        "nonce_header": nonce,
        "content_sha256_header": digest,
        "signature_header": sign_site_request(
            secret=_SECRET,
            timestamp=timestamp,
            nonce=nonce,
            method="POST",
            path=_PATH,
            body_sha256=digest,
        ),
    }


def test_settings_are_disabled_by_default_and_parse_pilot_user_ids() -> None:
    defaults = Settings()
    assert defaults.site_service_requests_ingest_enabled is False
    assert defaults.site_service_requests_bitrix_writes_enabled is False
    assert defaults.site_service_requests_outbound_replies_enabled is False

    configured = Settings(site_service_requests_first_line_user_ids="132252,12587")
    assert configured.site_service_requests_first_line_user_ids == [132252, 12587]


def test_signature_fixture_is_stable_for_php_contract() -> None:
    headers = _headers()
    assert headers["content_sha256_header"] == (
        "70de45086e16dfc8050b399626b7afc7f8f84084d33ff84ae33b182b21f552c9"
    )
    assert headers["signature_header"] == (
        "v1=a38241ff26b8956d165cd6f931299942a97b47dcea27ad00f61cbac62cf68b09"
    )


def test_valid_signature_reserves_nonce_and_replay_is_rejected() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        verified = verify_site_request(
            session,
            method="POST",
            path=_PATH,
            body=_BODY,
            settings=_settings(),
            now=_NOW,
            **_headers(),
        )
        session.commit()
        assert verified.nonce == _NONCE
        assert session.scalar(select(SiteServiceRequestNonce.nonce)) == _NONCE

        with pytest.raises(SiteServiceRequestAuthError, match="nonce_replay") as exc_info:
            verify_site_request(
                session,
                method="POST",
                path=_PATH,
                body=_BODY,
                settings=_settings(),
                now=_NOW,
                **_headers(),
            )
        assert exc_info.value.code == "nonce_replay"

    engine.dispose()


@pytest.mark.parametrize(
    ("override", "expected_code"),
    [
        ({"timestamp_header": str(_TIMESTAMP - 301)}, "timestamp_out_of_range"),
        ({"nonce_header": "not-a-uuid"}, "nonce_invalid"),
        ({"content_sha256_header": "0" * 64}, "content_hash_mismatch"),
        ({"signature_header": "v1=" + "0" * 64}, "signature_mismatch"),
    ],
)
def test_invalid_auth_material_is_rejected_without_reserving_nonce(
    override: dict[str, str],
    expected_code: str,
) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    headers = {**_headers(), **override}

    with Session(engine) as session:
        with pytest.raises(SiteServiceRequestAuthError) as exc_info:
            verify_site_request(
                session,
                method="POST",
                path=_PATH,
                body=_BODY,
                settings=_settings(),
                now=_NOW,
                **headers,
            )
        assert exc_info.value.code == expected_code
        assert session.scalar(select(SiteServiceRequestNonce.id)) is None

    engine.dispose()
