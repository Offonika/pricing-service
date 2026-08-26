from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials

from app.api.dependencies import security
from app.core.config import Settings, get_settings
from app.services.bitrix_procurement_order_formation_auth import normalize_bitrix_domain

TOKEN_ALG = "HS256"
TOKEN_TYP = "MM-LOGISTICS"
TOKEN_SCOPE = "logistics"


@dataclass(frozen=True)
class LogisticsBitrixSession:
    actor_user_id: int
    domain: str
    member_id: str
    bitrix_user_id: str
    expires_at: datetime


@dataclass(frozen=True)
class BitrixUser:
    user_id: str
    name: str | None


def _normalized(values: list[str]) -> set[str]:
    return {str(value).strip().lower() for value in values if str(value).strip()}


def ensure_logistics_bitrix_launch_allowed(
    *,
    domain: str,
    member_id: str,
    settings: Settings | None = None,
) -> tuple[str, str]:
    settings = settings or get_settings()
    if not settings.logistics_bitrix_app_enabled:
        raise HTTPException(status_code=403, detail="Bitrix logistics app is disabled")
    if not settings.logistics_bitrix_session_secret:
        raise HTTPException(status_code=500, detail="Bitrix logistics session is not configured")
    normalized_domain = normalize_bitrix_domain(domain)
    normalized_member_id = member_id.strip()
    if normalized_domain not in _normalized(settings.logistics_bitrix_allowed_domains):
        raise HTTPException(status_code=403, detail="Bitrix domain is not allowed")
    if normalized_member_id.lower() not in _normalized(
        settings.logistics_bitrix_allowed_member_ids
    ):
        raise HTTPException(status_code=403, detail="Bitrix member_id is not allowed")
    return normalized_domain, normalized_member_id


def load_bitrix_current_user(
    *,
    domain: str,
    access_token: str,
    settings: Settings | None = None,
) -> BitrixUser:
    settings = settings or get_settings()
    request = urllib.request.Request(
        f"https://{normalize_bitrix_domain(domain)}/rest/user.current.json",
        data=json.dumps({"auth": access_token}).encode("utf-8"),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=settings.logistics_bitrix_rest_timeout_seconds,
        ) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise HTTPException(status_code=401, detail="Bitrix access token was rejected") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=502, detail="Bitrix user.current is unavailable") from exc
    if body.get("error"):
        raise HTTPException(status_code=401, detail="Bitrix access token is invalid")
    result = body.get("result")
    if not isinstance(result, dict) or result.get("ID") in (None, ""):
        raise HTTPException(status_code=502, detail="Bitrix user.current returned invalid payload")
    name = " ".join(
        part
        for part in (
            str(result.get("NAME") or "").strip(),
            str(result.get("LAST_NAME") or "").strip(),
        )
        if part
    )
    return BitrixUser(user_id=str(result["ID"]).strip(), name=name or None)


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(raw: str) -> bytes:
    return base64.urlsafe_b64decode((raw + "=" * (-len(raw) % 4)).encode("ascii"))


def _secret(settings: Settings) -> bytes:
    value = settings.logistics_bitrix_session_secret
    if not value:
        raise HTTPException(status_code=500, detail="Bitrix logistics session is not configured")
    return value.encode("utf-8")


def _sign(value: str, settings: Settings) -> str:
    return _b64encode(hmac.new(_secret(settings), value.encode("ascii"), hashlib.sha256).digest())


def create_logistics_bitrix_session_token(
    *,
    actor_user_id: int,
    domain: str,
    member_id: str,
    bitrix_user_id: str,
    settings: Settings | None = None,
    now: int | None = None,
) -> tuple[str, datetime]:
    settings = settings or get_settings()
    ensure_logistics_bitrix_launch_allowed(
        domain=domain,
        member_id=member_id,
        settings=settings,
    )
    issued_at = int(time.time() if now is None else now)
    expires_at = issued_at + int(settings.logistics_bitrix_session_ttl_seconds)
    header = _b64encode(
        json.dumps({"alg": TOKEN_ALG, "typ": TOKEN_TYP}, separators=(",", ":")).encode()
    )
    payload = _b64encode(
        json.dumps(
            {
                "scope": TOKEN_SCOPE,
                "actor_user_id": actor_user_id,
                "domain": domain,
                "member_id": member_id,
                "bitrix_user_id": bitrix_user_id,
                "iat": issued_at,
                "exp": expires_at,
            },
            separators=(",", ":"),
        ).encode()
    )
    signing_input = f"{header}.{payload}"
    return (
        f"{signing_input}.{_sign(signing_input, settings)}",
        datetime.fromtimestamp(expires_at, UTC),
    )


def verify_logistics_bitrix_session_token(
    token: str,
    *,
    settings: Settings | None = None,
    now: int | None = None,
) -> LogisticsBitrixSession:
    settings = settings or get_settings()
    try:
        header_raw, payload_raw, signature = token.split(".", 2)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="invalid logistics session") from exc
    signing_input = f"{header_raw}.{payload_raw}"
    if not hmac.compare_digest(signature, _sign(signing_input, settings)):
        raise HTTPException(status_code=401, detail="invalid logistics session")
    try:
        header = json.loads(_b64decode(header_raw))
        payload: dict[str, Any] = json.loads(_b64decode(payload_raw))
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=401, detail="invalid logistics session") from exc
    if header.get("alg") != TOKEN_ALG or header.get("typ") != TOKEN_TYP:
        raise HTTPException(status_code=401, detail="invalid logistics session")
    if payload.get("scope") != TOKEN_SCOPE:
        raise HTTPException(status_code=401, detail="invalid logistics session")
    current = int(time.time() if now is None else now)
    expires_at = int(payload.get("exp") or 0)
    if expires_at <= current:
        raise HTTPException(status_code=401, detail="logistics session expired")
    domain, member_id = ensure_logistics_bitrix_launch_allowed(
        domain=str(payload.get("domain") or ""),
        member_id=str(payload.get("member_id") or ""),
        settings=settings,
    )
    try:
        actor_user_id = int(payload["actor_user_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=401, detail="invalid logistics session") from exc
    bitrix_user_id = str(payload.get("bitrix_user_id") or "").strip()
    if not bitrix_user_id:
        raise HTTPException(status_code=401, detail="invalid logistics session")
    return LogisticsBitrixSession(
        actor_user_id=actor_user_id,
        domain=domain,
        member_id=member_id,
        bitrix_user_id=bitrix_user_id,
        expires_at=datetime.fromtimestamp(expires_at, UTC),
    )


def verify_logistics_bitrix_session(
    credentials: HTTPAuthorizationCredentials | None = Security(security),  # noqa: B008
) -> LogisticsBitrixSession:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="unauthorized")
    return verify_logistics_bitrix_session_token(credentials.credentials)
