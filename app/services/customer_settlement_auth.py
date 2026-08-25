from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models.customer_settlement import CustomerSettlementAssertionJti
from app.services.customer_settlements import normalize_site_user_id

TOKEN_ALGORITHM = "HS256"
TOKEN_TYPE = "MM-CUSTOMER-SETTLEMENTS"
TOKEN_SCOPE = "customer:settlements:read"
_JTI_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


class CustomerSettlementAuthError(RuntimeError):
    def __init__(self, code: str = "invalid") -> None:
        self.code = code
        super().__init__("invalid assertion")


class CustomerSettlementAuthConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class CustomerSettlementIdentity:
    site_user_id: str
    issuer: str
    audience: str
    scope: str
    jti_hash: str
    expires_at: datetime


def _b64_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def _keys(settings: Settings) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    active_kid = settings.customer_settlements_assertion_active_kid
    previous_kid = settings.customer_settlements_assertion_previous_kid
    if active_kid and previous_kid and active_kid == previous_kid:
        raise CustomerSettlementAuthConfigError("assertion key ids must be unique")
    if active_kid:
        if not settings.customer_settlements_assertion_active_secret:
            raise CustomerSettlementAuthConfigError("active assertion secret is not configured")
        result[active_kid] = settings.customer_settlements_assertion_active_secret.encode("utf-8")
    if previous_kid:
        if not settings.customer_settlements_assertion_previous_secret:
            raise CustomerSettlementAuthConfigError("previous assertion secret is not configured")
        result[previous_kid] = settings.customer_settlements_assertion_previous_secret.encode(
            "utf-8"
        )
    if not result:
        raise CustomerSettlementAuthConfigError("assertion keys are not configured")
    return result


def _sign(signing_input: str, secret: bytes) -> str:
    digest = hmac.new(secret, signing_input.encode("ascii"), hashlib.sha256).digest()
    return _b64_encode(digest)


def _source_ip_allowed(source_ip: str | None, allowed_values: list[str]) -> bool:
    if not allowed_values:
        raise CustomerSettlementAuthConfigError("allowed source IPs are not configured")
    if not source_ip:
        return False
    try:
        address = ipaddress.ip_address(source_ip)
    except ValueError:
        return False
    for raw_value in allowed_values:
        try:
            network = ipaddress.ip_network(str(raw_value).strip(), strict=False)
        except ValueError as exc:
            raise CustomerSettlementAuthConfigError("allowed source IP is invalid") from exc
        if address in network:
            return True
    return False


def create_customer_settlement_assertion(
    *,
    site_user_id: str | int,
    settings: Settings | None = None,
    now: int | None = None,
    jti: str | None = None,
) -> tuple[str, int]:
    settings = settings or get_settings()
    key_id = settings.customer_settlements_assertion_active_kid
    if not key_id:
        raise CustomerSettlementAuthConfigError("active assertion key id is not configured")
    keys = _keys(settings)
    issued_at = int(now if now is not None else time.time())
    ttl = int(settings.customer_settlements_assertion_ttl_seconds)
    if ttl < 1 or ttl > 60:
        raise CustomerSettlementAuthConfigError("assertion TTL must be between 1 and 60 seconds")
    expires_at = issued_at + ttl
    resolved_jti = jti or secrets.token_urlsafe(24)
    if not _JTI_RE.fullmatch(resolved_jti):
        raise ValueError("jti must contain 16-128 URL-safe characters")
    user_id = normalize_site_user_id(site_user_id)
    header = {"alg": TOKEN_ALGORITHM, "typ": TOKEN_TYPE, "kid": key_id}
    payload = {
        "iss": settings.customer_settlements_assertion_issuer,
        "aud": settings.customer_settlements_assertion_audience,
        "sub": user_id,
        "site_user_id": user_id,
        "scope": TOKEN_SCOPE,
        "iat": issued_at,
        "nbf": issued_at,
        "exp": expires_at,
        "jti": resolved_jti,
    }
    header_raw = _b64_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_raw = _b64_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header_raw}.{payload_raw}"
    return f"{signing_input}.{_sign(signing_input, keys[key_id])}", expires_at


def _integer_claim(payload: dict[str, Any], name: str) -> int:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise CustomerSettlementAuthError()
    return value


def verify_and_consume_customer_settlement_assertion(
    session: Session,
    *,
    token: str,
    source_ip: str | None,
    settings: Settings | None = None,
    now: int | None = None,
) -> CustomerSettlementIdentity:
    settings = settings or get_settings()
    if len(token) > 4096:
        raise CustomerSettlementAuthError()
    if not _source_ip_allowed(source_ip, settings.customer_settlements_allowed_source_ips):
        raise CustomerSettlementAuthError()
    try:
        header_raw, payload_raw, signature = token.split(".", 2)
        header = json.loads(_b64_decode(header_raw).decode("utf-8"))
        payload = json.loads(_b64_decode(payload_raw).decode("utf-8"))
    except (
        binascii.Error,
        UnicodeDecodeError,
        UnicodeEncodeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        raise CustomerSettlementAuthError() from exc
    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise CustomerSettlementAuthError()
    if header.get("alg") != TOKEN_ALGORITHM or header.get("typ") != TOKEN_TYPE:
        raise CustomerSettlementAuthError()
    key_id = str(header.get("kid") or "")
    secret = _keys(settings).get(key_id)
    if secret is None:
        raise CustomerSettlementAuthError()
    signing_input = f"{header_raw}.{payload_raw}"
    if not hmac.compare_digest(signature, _sign(signing_input, secret)):
        raise CustomerSettlementAuthError()

    issuer = str(payload.get("iss") or "")
    audience = str(payload.get("aud") or "")
    if issuer != settings.customer_settlements_assertion_issuer:
        raise CustomerSettlementAuthError()
    if audience != settings.customer_settlements_assertion_audience:
        raise CustomerSettlementAuthError()
    scope = str(payload.get("scope") or "")
    if scope != TOKEN_SCOPE:
        raise CustomerSettlementAuthError("scope")
    try:
        site_user_id = normalize_site_user_id(payload.get("site_user_id") or "")
    except ValueError as exc:
        raise CustomerSettlementAuthError() from exc
    if str(payload.get("sub") or "") != site_user_id:
        raise CustomerSettlementAuthError()
    jti = str(payload.get("jti") or "")
    if not _JTI_RE.fullmatch(jti):
        raise CustomerSettlementAuthError()

    issued_at = _integer_claim(payload, "iat")
    not_before = _integer_claim(payload, "nbf")
    expires_at = _integer_claim(payload, "exp")
    current_ts = int(now if now is not None else time.time())
    skew = int(settings.customer_settlements_assertion_clock_skew_seconds)
    max_ttl = int(settings.customer_settlements_assertion_ttl_seconds)
    if skew < 0 or skew > 30 or max_ttl < 1 or max_ttl > 60:
        raise CustomerSettlementAuthConfigError("assertion timing settings are invalid")
    if expires_at <= issued_at or expires_at - issued_at > max_ttl:
        raise CustomerSettlementAuthError()
    if not_before < issued_at or not_before >= expires_at:
        raise CustomerSettlementAuthError()
    if issued_at > current_ts + skew or not_before > current_ts + skew:
        raise CustomerSettlementAuthError("future")
    if expires_at <= current_ts - skew:
        raise CustomerSettlementAuthError("expired")

    jti_hash = hashlib.sha256(jti.encode("utf-8")).hexdigest()
    consumed_at = datetime.fromtimestamp(current_ts, tz=UTC)
    expires_at_dt = datetime.fromtimestamp(expires_at, tz=UTC)
    try:
        with session.begin_nested():
            session.add(
                CustomerSettlementAssertionJti(
                    jti_hash=jti_hash,
                    expires_at=expires_at_dt,
                    consumed_at=consumed_at,
                )
            )
            session.flush()
    except IntegrityError as exc:
        raise CustomerSettlementAuthError("replay") from exc
    return CustomerSettlementIdentity(
        site_user_id=site_user_id,
        issuer=issuer,
        audience=audience,
        scope=scope,
        jti_hash=jti_hash,
        expires_at=expires_at_dt,
    )
