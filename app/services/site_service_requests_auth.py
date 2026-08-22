from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.site_service_requests import SiteServiceRequestNonce

_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SIGNATURE = re.compile(r"^v1=([0-9a-f]{64})$")


class SiteServiceRequestAuthError(ValueError):
    """Stable authentication failure safe to expose without request details."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class VerifiedSiteRequest:
    timestamp: int
    nonce: str
    content_sha256: str
    body: bytes = field(repr=False)


def content_sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def build_signing_input(
    *,
    timestamp: int,
    nonce: str,
    method: str,
    path: str,
    body_sha256: str,
) -> str:
    return "\n".join(
        (
            "v1",
            str(timestamp),
            nonce,
            method.upper(),
            path,
            body_sha256,
        )
    )


def sign_site_request(
    *,
    secret: str,
    timestamp: int,
    nonce: str,
    method: str,
    path: str,
    body_sha256: str,
) -> str:
    signing_input = build_signing_input(
        timestamp=timestamp,
        nonce=nonce,
        method=method,
        path=path,
        body_sha256=body_sha256,
    )
    digest = hmac.new(
        secret.encode("utf-8"),
        signing_input.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"v1={digest}"


def verify_site_request(
    session: Session,
    *,
    method: str,
    path: str,
    body: bytes,
    timestamp_header: str,
    nonce_header: str,
    content_sha256_header: str,
    signature_header: str,
    settings: Settings,
    now: datetime | None = None,
) -> VerifiedSiteRequest:
    secret = str(settings.site_service_requests_hmac_secret or "")
    if not secret:
        raise SiteServiceRequestAuthError("auth_not_configured")

    timestamp = _parse_timestamp(timestamp_header)
    nonce = _parse_nonce(nonce_header)
    supplied_hash = _parse_content_hash(content_sha256_header)
    supplied_signature = _parse_signature(signature_header)
    current_time = _utc_now(now)

    if abs(current_time.timestamp() - timestamp) > float(
        settings.site_service_requests_timestamp_tolerance_seconds
    ):
        raise SiteServiceRequestAuthError("timestamp_out_of_range")

    calculated_hash = content_sha256(body)
    if not hmac.compare_digest(supplied_hash, calculated_hash):
        raise SiteServiceRequestAuthError("content_hash_mismatch")

    expected = sign_site_request(
        secret=secret,
        timestamp=timestamp,
        nonce=nonce,
        method=method,
        path=path,
        body_sha256=calculated_hash,
    )
    if not hmac.compare_digest(supplied_signature, expected.removeprefix("v1=")):
        raise SiteServiceRequestAuthError("signature_mismatch")

    _reserve_nonce(
        session,
        nonce=nonce,
        now=current_time,
        ttl_seconds=settings.site_service_requests_nonce_ttl_seconds,
    )
    return VerifiedSiteRequest(
        timestamp=timestamp,
        nonce=nonce,
        content_sha256=calculated_hash,
        body=body,
    )


def _parse_timestamp(value: str) -> int:
    normalized = str(value or "").strip()
    try:
        timestamp = int(normalized)
    except ValueError as exc:
        raise SiteServiceRequestAuthError("timestamp_invalid") from exc
    if normalized != str(timestamp) or timestamp < 0:
        raise SiteServiceRequestAuthError("timestamp_invalid")
    return timestamp


def _parse_nonce(value: str) -> str:
    normalized = str(value or "").strip().lower()
    try:
        parsed = UUID(normalized)
    except ValueError as exc:
        raise SiteServiceRequestAuthError("nonce_invalid") from exc
    if parsed.version is None or str(parsed) != normalized:
        raise SiteServiceRequestAuthError("nonce_invalid")
    return normalized


def _parse_content_hash(value: str) -> str:
    normalized = str(value or "").strip()
    if not _HEX_SHA256.fullmatch(normalized):
        raise SiteServiceRequestAuthError("content_hash_invalid")
    return normalized


def _parse_signature(value: str) -> str:
    normalized = str(value or "").strip()
    match = _SIGNATURE.fullmatch(normalized)
    if match is None:
        raise SiteServiceRequestAuthError("signature_invalid")
    return match.group(1)


def _utc_now(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        return current.replace(tzinfo=UTC)
    return current.astimezone(UTC)


def _reserve_nonce(
    session: Session,
    *,
    nonce: str,
    now: datetime,
    ttl_seconds: int,
) -> None:
    session.execute(
        delete(SiteServiceRequestNonce).where(SiteServiceRequestNonce.expires_at <= now)
    )
    try:
        with session.begin_nested():
            session.add(
                SiteServiceRequestNonce(
                    nonce=nonce,
                    expires_at=now + timedelta(seconds=ttl_seconds),
                )
            )
            session.flush()
    except IntegrityError as exc:
        raise SiteServiceRequestAuthError("nonce_replay") from exc
