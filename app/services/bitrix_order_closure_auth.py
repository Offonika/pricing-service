from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials

from app.api.dependencies import security
from app.core.config import Settings, get_settings
from app.services.bitrix_procurement_order_formation_auth import (
    ensure_bitrix_launch_allowed as _ensure_procurement_launch_allowed,
)
from app.services.bitrix_procurement_order_formation_auth import (
    load_bitrix_current_user as load_bitrix_current_user,
)

_TOKEN_TYP = "MM-ORDER-CLOSURE"
_TOKEN_SCOPE = "order_closure"


@dataclass(frozen=True)
class OrderClosureSession:
    actor: str
    domain: str
    member_id: str
    user_id: str
    user_name: str
    can_confirm: bool
    expires_at: datetime


def _normal_set(values: list[str]) -> set[str]:
    return {str(value).strip().lower() for value in values if str(value).strip()}


def _ensure_enabled(settings: Settings) -> None:
    if not settings.order_closure_bitrix_enabled:
        raise HTTPException(status_code=403, detail="Bitrix order closure app is disabled")
    if not settings.order_closure_bitrix_session_secret:
        raise HTTPException(
            status_code=500, detail="order closure session secret is not configured"
        )


def ensure_order_closure_launch_allowed(
    *, domain: str, member_id: str, settings: Settings | None = None
) -> tuple[str, str]:
    settings = settings or get_settings()
    _ensure_enabled(settings)
    # Reuse the hardened domain parser and then apply this feature's own allowlists.
    normalized_domain, normalized_member = _ensure_procurement_launch_allowed(
        domain=domain,
        member_id=member_id,
        settings=settings.model_copy(
            update={
                "procurement_order_formation_bitrix_enabled": True,
                "procurement_order_formation_bitrix_session_secret": (
                    settings.order_closure_bitrix_session_secret
                ),
                "procurement_order_formation_bitrix_allowed_domains": (
                    settings.order_closure_bitrix_allowed_domains
                ),
                "procurement_order_formation_bitrix_allowed_member_ids": (
                    settings.order_closure_bitrix_allowed_member_ids
                ),
            }
        ),
    )
    return normalized_domain, normalized_member


def ensure_order_closure_user_allowed(user_id: str, *, settings: Settings) -> tuple[str, bool]:
    normalized = str(user_id).strip()
    viewers = _normal_set(settings.order_closure_bitrix_allowed_user_ids)
    operators = _normal_set(settings.order_closure_operator_user_ids)
    if normalized.lower() not in viewers | operators:
        raise HTTPException(status_code=403, detail="Bitrix user is not allowed")
    return normalized, normalized.lower() in operators


def _b64_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode((value + "=" * (-len(value) % 4)).encode("ascii"))


def _secret(settings: Settings) -> bytes:
    value = settings.order_closure_bitrix_session_secret
    if not value:
        raise HTTPException(
            status_code=500, detail="order closure session secret is not configured"
        )
    return value.encode("utf-8")


def _sign(value: str, settings: Settings) -> str:
    return _b64_encode(hmac.new(_secret(settings), value.encode("ascii"), hashlib.sha256).digest())


def create_order_closure_session_token(
    *,
    domain: str,
    member_id: str,
    user_id: str,
    user_name: str | None,
    can_confirm: bool,
    settings: Settings | None = None,
    now: int | None = None,
) -> tuple[str, datetime]:
    settings = settings or get_settings()
    _ensure_enabled(settings)
    issued_at = int(now if now is not None else time.time())
    expires_at = issued_at + int(settings.order_closure_bitrix_session_ttl_seconds)
    header = _b64_encode(json.dumps({"alg": "HS256", "typ": _TOKEN_TYP}).encode("utf-8"))
    payload: dict[str, Any] = {
        "sub": f"bitrix:{member_id}:{user_id}",
        "scope": _TOKEN_SCOPE,
        "domain": domain,
        "member_id": member_id,
        "user_id": user_id,
        "user_name": user_name or "",
        "can_confirm": bool(can_confirm),
        "iat": issued_at,
        "exp": expires_at,
    }
    encoded = _b64_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header}.{encoded}"
    return f"{signing_input}.{_sign(signing_input, settings)}", datetime.fromtimestamp(
        expires_at, UTC
    )


def verify_order_closure_session_token(
    token: str, *, settings: Settings | None = None, now: int | None = None
) -> OrderClosureSession:
    settings = settings or get_settings()
    _ensure_enabled(settings)
    try:
        header_raw, payload_raw, signature = token.split(".", 2)
        signing_input = f"{header_raw}.{payload_raw}"
        if not hmac.compare_digest(signature, _sign(signing_input, settings)):
            raise ValueError
        header = json.loads(_b64_decode(header_raw).decode("utf-8"))
        payload = json.loads(_b64_decode(payload_raw).decode("utf-8"))
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=401, detail="invalid order closure session") from exc
    if header.get("typ") != _TOKEN_TYP or payload.get("scope") != _TOKEN_SCOPE:
        raise HTTPException(status_code=401, detail="invalid order closure session")
    current = int(now if now is not None else time.time())
    expires_at = int(payload.get("exp") or 0)
    if expires_at <= current:
        raise HTTPException(status_code=401, detail="order closure session expired")
    domain, member_id = ensure_order_closure_launch_allowed(
        domain=str(payload.get("domain") or ""),
        member_id=str(payload.get("member_id") or ""),
        settings=settings,
    )
    user_id, is_operator = ensure_order_closure_user_allowed(
        str(payload.get("user_id") or ""), settings=settings
    )
    can_confirm = is_operator and settings.order_closure_apply_enabled
    if bool(payload.get("can_confirm")) != can_confirm:
        raise HTTPException(status_code=401, detail="order closure permissions changed")
    return OrderClosureSession(
        actor=f"bitrix:{member_id}:{user_id}",
        domain=domain,
        member_id=member_id,
        user_id=user_id,
        user_name=str(payload.get("user_name") or ""),
        can_confirm=can_confirm,
        expires_at=datetime.fromtimestamp(expires_at, UTC),
    )


def verify_order_closure_session(
    credentials: HTTPAuthorizationCredentials | None = Security(security),  # noqa: B008
) -> OrderClosureSession:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="unauthorized")
    return verify_order_closure_session_token(credentials.credentials)
