from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials

from app.api.dependencies import security
from app.core.config import Settings, get_settings

_TOKEN_ALG = "HS256"
_TOKEN_TYP = "MM-PROCUREMENT-LABELS"
_TOKEN_SCOPE = "procurement_labels"
_DOMAIN_RE = re.compile(r"^[a-z0-9.-]+$", re.IGNORECASE)


@dataclass(frozen=True)
class ProcurementLabelsSession:
    actor: str
    domain: str
    member_id: str
    user_id: str
    expires_at: datetime
    user_name: str = ""


@dataclass(frozen=True)
class BitrixUser:
    user_id: str
    name: str | None


def normalize_bitrix_domain(value: str) -> str:
    raw = value.strip().lower()
    if "://" in raw:
        parsed = urllib.parse.urlparse(raw)
        raw = parsed.netloc
    raw = raw.split("/", 1)[0].strip()
    if not raw or not _DOMAIN_RE.fullmatch(raw):
        raise HTTPException(status_code=400, detail="invalid Bitrix domain")
    return raw


def _normal_set(values: list[str]) -> set[str]:
    return {str(value).strip().lower() for value in values if str(value).strip()}


def _ensure_enabled(settings: Settings) -> None:
    if not settings.procurement_labels_bitrix_enabled:
        raise HTTPException(status_code=403, detail="Bitrix procurement labels app is disabled")
    if not settings.procurement_labels_bitrix_session_secret:
        raise HTTPException(
            status_code=500,
            detail="Bitrix procurement labels session secret is not configured",
        )


def ensure_bitrix_launch_allowed(
    *,
    domain: str,
    member_id: str,
    settings: Settings | None = None,
) -> tuple[str, str]:
    settings = settings or get_settings()
    _ensure_enabled(settings)
    normalized_domain = normalize_bitrix_domain(domain)
    normalized_member_id = member_id.strip()
    if normalized_domain not in _normal_set(settings.procurement_labels_bitrix_allowed_domains):
        raise HTTPException(status_code=403, detail="Bitrix domain is not allowed")
    if normalized_member_id.lower() not in _normal_set(
        settings.procurement_labels_bitrix_allowed_member_ids
    ):
        raise HTTPException(status_code=403, detail="Bitrix member_id is not allowed")
    return normalized_domain, normalized_member_id


def ensure_bitrix_user_allowed(user_id: str, *, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    normalized_user_id = str(user_id).strip()
    allowed_user_ids = _normal_set(settings.procurement_labels_bitrix_allowed_user_ids)
    if allowed_user_ids and normalized_user_id.lower() not in allowed_user_ids:
        raise HTTPException(status_code=403, detail="Bitrix user is not allowed")
    return normalized_user_id


def load_bitrix_current_user(
    *,
    domain: str,
    access_token: str,
    settings: Settings | None = None,
) -> BitrixUser:
    settings = settings or get_settings()
    normalized_domain = normalize_bitrix_domain(domain)
    payload = json.dumps({"auth": access_token}).encode("utf-8")
    request = urllib.request.Request(
        f"https://{normalized_domain}/rest/user.current.json",
        data=payload,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=settings.procurement_labels_bitrix_rest_timeout_seconds,
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

    name_parts = [
        str(result.get("NAME") or "").strip(),
        str(result.get("LAST_NAME") or "").strip(),
    ]
    name = " ".join(part for part in name_parts if part) or None
    return BitrixUser(user_id=str(result["ID"]).strip(), name=name)


def _b64_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def _secret(settings: Settings) -> bytes:
    secret = settings.procurement_labels_bitrix_session_secret
    if not secret:
        raise HTTPException(
            status_code=500,
            detail="Bitrix procurement labels session secret is not configured",
        )
    return secret.encode("utf-8")


def _sign(signing_input: str, *, settings: Settings) -> str:
    signature = hmac.new(_secret(settings), signing_input.encode("ascii"), hashlib.sha256).digest()
    return _b64_encode(signature)


def create_procurement_labels_session_token(
    *,
    domain: str,
    member_id: str,
    user_id: str,
    user_name: str | None = None,
    settings: Settings | None = None,
    now: int | None = None,
) -> tuple[str, datetime]:
    settings = settings or get_settings()
    _ensure_enabled(settings)
    issued_at = int(now if now is not None else time.time())
    expires_at_ts = issued_at + int(settings.procurement_labels_bitrix_session_ttl_seconds)
    payload: dict[str, Any] = {
        "sub": f"bitrix:{member_id}:{user_id}",
        "scope": _TOKEN_SCOPE,
        "domain": domain,
        "member_id": member_id,
        "user_id": user_id,
        "iat": issued_at,
        "exp": expires_at_ts,
    }
    if user_name:
        payload["user_name"] = user_name
    header = {"alg": _TOKEN_ALG, "typ": _TOKEN_TYP}
    header_raw = _b64_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_raw = _b64_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header_raw}.{payload_raw}"
    token = f"{signing_input}.{_sign(signing_input, settings=settings)}"
    return token, datetime.fromtimestamp(expires_at_ts, UTC)


def verify_procurement_labels_session_token(
    token: str,
    *,
    settings: Settings | None = None,
    now: int | None = None,
) -> ProcurementLabelsSession:
    settings = settings or get_settings()
    _ensure_enabled(settings)
    try:
        header_raw, payload_raw, signature = token.split(".", 2)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="invalid procurement labels session") from exc

    signing_input = f"{header_raw}.{payload_raw}"
    expected_signature = _sign(signing_input, settings=settings)
    if not hmac.compare_digest(signature, expected_signature):
        raise HTTPException(status_code=401, detail="invalid procurement labels session")

    try:
        header = json.loads(_b64_decode(header_raw).decode("utf-8"))
        payload = json.loads(_b64_decode(payload_raw).decode("utf-8"))
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=401, detail="invalid procurement labels session") from exc

    if header.get("alg") != _TOKEN_ALG or header.get("typ") != _TOKEN_TYP:
        raise HTTPException(status_code=401, detail="invalid procurement labels session")
    if payload.get("scope") != _TOKEN_SCOPE:
        raise HTTPException(status_code=401, detail="invalid procurement labels session")

    current_ts = int(now if now is not None else time.time())
    expires_at_ts = int(payload.get("exp") or 0)
    if expires_at_ts <= current_ts:
        raise HTTPException(status_code=401, detail="procurement labels session expired")

    domain, member_id = ensure_bitrix_launch_allowed(
        domain=str(payload.get("domain") or ""),
        member_id=str(payload.get("member_id") or ""),
        settings=settings,
    )
    user_id = ensure_bitrix_user_allowed(str(payload.get("user_id") or ""), settings=settings)
    return ProcurementLabelsSession(
        actor=f"bitrix:{member_id}:{user_id}",
        domain=domain,
        member_id=member_id,
        user_id=user_id,
        expires_at=datetime.fromtimestamp(expires_at_ts, UTC),
        user_name=str(payload.get("user_name") or "").strip(),
    )


def verify_procurement_labels_session(
    credentials: HTTPAuthorizationCredentials | None = Security(security),
) -> ProcurementLabelsSession:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="unauthorized")
    return verify_procurement_labels_session_token(credentials.credentials)
