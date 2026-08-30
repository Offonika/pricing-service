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

_DOMAIN_RE = re.compile(r"^[a-z0-9.-]+$", re.IGNORECASE)
_TOKEN_TYPE = "MM-SITE-SERVICE-UI"
_TOKEN_SCOPE = "site_service_requests_ui"
_PLACEMENT = "CRM_DYNAMIC_1134_DETAIL_TAB"


@dataclass(frozen=True)
class SiteServiceRequestUiSession:
    domain: str
    member_id: str
    user_id: int
    user_name: str
    is_admin: bool
    item_id: int
    expires_at: datetime


def _normalize_domain(value: str) -> str:
    raw = value.strip().lower()
    if "://" in raw:
        raw = urllib.parse.urlparse(raw).netloc
    raw = raw.split("/", 1)[0].strip()
    if not raw or _DOMAIN_RE.fullmatch(raw) is None:
        raise HTTPException(status_code=400, detail="invalid_bitrix_domain")
    return raw


def _normal_set(values: list[str]) -> set[str]:
    return {str(value).strip().lower() for value in values if str(value).strip()}


def _strict_positive_int(value: object, *, error_detail: str) -> int:
    if type(value) is int:
        parsed = value
    elif isinstance(value, str) and value.isascii() and value.isdigit():
        parsed = int(value)
    else:
        raise HTTPException(status_code=502, detail=error_detail)
    if parsed <= 0:
        raise HTTPException(status_code=502, detail=error_detail)
    return parsed


def _ensure_enabled(settings: Settings) -> None:
    if not settings.site_service_requests_ui_enabled:
        raise HTTPException(status_code=403, detail="site_service_requests_ui_disabled")
    if not settings.site_service_requests_ui_session_secret:
        raise HTTPException(status_code=503, detail="ui_session_not_configured")


def validate_site_service_request_ui_launch(
    *,
    domain: str,
    member_id: str,
    placement: str,
    settings: Settings,
) -> tuple[str, str]:
    _ensure_enabled(settings)
    normalized_domain = _normalize_domain(domain)
    normalized_member_id = member_id.strip()
    if placement != _PLACEMENT:
        raise HTTPException(status_code=400, detail="invalid_placement")
    if normalized_domain not in _normal_set(settings.site_service_requests_ui_allowed_domains):
        raise HTTPException(status_code=403, detail="bitrix_domain_not_allowed")
    if normalized_member_id.lower() not in _normal_set(
        settings.site_service_requests_ui_allowed_member_ids
    ):
        raise HTTPException(status_code=403, detail="bitrix_member_not_allowed")
    return normalized_domain, normalized_member_id


def _bitrix_call(*, domain: str, access_token: str, method: str, params: dict[str, Any]) -> Any:
    body = json.dumps({**params, "auth": access_token}).encode("utf-8")
    request = urllib.request.Request(
        f"https://{domain}/rest/{method}.json",
        data=body,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw_payload = response.read(1024 * 1024 + 1)
        if len(raw_payload) > 1024 * 1024:
            raise ValueError("Bitrix response is too large")
        payload = json.loads(raw_payload.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            raise HTTPException(status_code=401, detail="bitrix_access_token_rejected") from exc
        raise HTTPException(status_code=502, detail="bitrix_ui_preflight_unavailable") from exc
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=502, detail="bitrix_ui_preflight_unavailable") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="bitrix_ui_preflight_unavailable")
    if payload.get("error"):
        error_code = str(payload.get("error") or "").casefold()
        if "token" in error_code or "auth" in error_code:
            raise HTTPException(status_code=401, detail="bitrix_access_token_rejected")
        raise HTTPException(status_code=403, detail="bitrix_ui_access_denied")
    return payload.get("result")


def authenticate_site_service_request_ui_user(
    *,
    domain: str,
    access_token: str,
    item_id: int,
    settings: Settings,
) -> tuple[int, str, bool]:
    user = _bitrix_call(domain=domain, access_token=access_token, method="user.current", params={})
    if not isinstance(user, dict):
        raise HTTPException(status_code=502, detail="bitrix_user_payload_invalid")
    user_id = _strict_positive_int(user.get("ID"), error_detail="bitrix_user_payload_invalid")
    name_parts = (user.get("NAME"), user.get("LAST_NAME"))
    if any(value is not None and not isinstance(value, str) for value in name_parts):
        raise HTTPException(status_code=502, detail="bitrix_user_payload_invalid")
    name = (
        " ".join(
            part
            for part in (str(name_parts[0] or "").strip(), str(name_parts[1] or "").strip())
            if part
        )
        or f"Сотрудник #{user_id}"
    )
    if len(name) > 255:
        raise HTTPException(status_code=502, detail="bitrix_user_payload_invalid")
    raw_admin = user.get("ADMIN", False)
    if type(raw_admin) is bool:
        is_admin = raw_admin
    elif isinstance(raw_admin, str) and raw_admin in {"Y", "N"}:
        is_admin = raw_admin == "Y"
    else:
        raise HTTPException(status_code=502, detail="bitrix_user_payload_invalid")
    allowed = set(settings.site_service_requests_ui_allowed_user_ids)
    if not is_admin and user_id not in allowed:
        raise HTTPException(status_code=403, detail="support_user_not_allowed")

    item = _bitrix_call(
        domain=domain,
        access_token=access_token,
        method="crm.item.get",
        params={
            "entityTypeId": settings.site_service_requests_bitrix_entity_type_id,
            "id": item_id,
        },
    )
    if not isinstance(item, dict) or not isinstance(item.get("item"), dict):
        raise HTTPException(status_code=403, detail="service_item_not_accessible")
    actual_id_values = [item["item"][key] for key in ("id", "ID") if key in item["item"]]
    if not actual_id_values or any(
        _strict_positive_int(value, error_detail="service_item_payload_invalid") != item_id
        for value in actual_id_values
    ):
        raise HTTPException(status_code=403, detail="service_item_not_accessible")
    return user_id, name, is_admin


def _b64_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64_decode(raw: str) -> bytes:
    return base64.urlsafe_b64decode((raw + "=" * (-len(raw) % 4)).encode("ascii"))


def _secret(settings: Settings) -> bytes:
    value = settings.site_service_requests_ui_session_secret
    if not value or len(value.encode("utf-8")) < 32:
        raise HTTPException(status_code=503, detail="ui_session_not_configured")
    return value.encode("utf-8")


def create_site_service_request_ui_session_token(
    *,
    domain: str,
    member_id: str,
    user_id: int,
    user_name: str,
    is_admin: bool,
    item_id: int,
    settings: Settings,
    now: int | None = None,
) -> tuple[str, datetime]:
    _ensure_enabled(settings)
    issued_at = int(time.time() if now is None else now)
    expires = issued_at + settings.site_service_requests_ui_session_ttl_seconds
    header = {"alg": "HS256", "typ": _TOKEN_TYPE}
    payload = {
        "scope": _TOKEN_SCOPE,
        "domain": domain,
        "member_id": member_id,
        "user_id": user_id,
        "user_name": user_name,
        "is_admin": is_admin,
        "item_id": item_id,
        "iat": issued_at,
        "exp": expires,
    }
    header_raw = _b64_encode(json.dumps(header, separators=(",", ":")).encode())
    payload_raw = _b64_encode(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{header_raw}.{payload_raw}"
    signature = _b64_encode(
        hmac.new(_secret(settings), signing_input.encode("ascii"), hashlib.sha256).digest()
    )
    return f"{signing_input}.{signature}", datetime.fromtimestamp(expires, UTC)


def verify_site_service_request_ui_session_token(
    token: str, *, settings: Settings | None = None, now: int | None = None
) -> SiteServiceRequestUiSession:
    settings = settings or get_settings()
    _ensure_enabled(settings)
    try:
        header_raw, payload_raw, signature = token.split(".", 2)
        signing_input = f"{header_raw}.{payload_raw}"
        expected = _b64_encode(
            hmac.new(_secret(settings), signing_input.encode("ascii"), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        header = json.loads(_b64_decode(header_raw))
        payload = json.loads(_b64_decode(payload_raw))
        if header != {"alg": "HS256", "typ": _TOKEN_TYPE}:
            raise ValueError
        if payload.get("scope") != _TOKEN_SCOPE:
            raise ValueError
        expires = int(payload["exp"])
        if expires <= int(time.time() if now is None else now):
            raise ValueError
        domain = _normalize_domain(str(payload["domain"]))
        member_id = str(payload["member_id"]).strip()
        if domain not in _normal_set(settings.site_service_requests_ui_allowed_domains):
            raise ValueError
        if member_id.lower() not in _normal_set(
            settings.site_service_requests_ui_allowed_member_ids
        ):
            raise ValueError
        user_id = _strict_positive_int(
            payload["user_id"], error_detail="invalid_site_service_ui_session"
        )
        item_id = _strict_positive_int(
            payload["item_id"], error_detail="invalid_site_service_ui_session"
        )
    except HTTPException as exc:
        raise HTTPException(status_code=401, detail="invalid_site_service_ui_session") from exc
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=401, detail="invalid_site_service_ui_session") from exc
    return SiteServiceRequestUiSession(
        domain=domain,
        member_id=member_id,
        user_id=user_id,
        user_name=str(payload.get("user_name") or f"Сотрудник #{user_id}"),
        is_admin=bool(payload.get("is_admin")),
        item_id=item_id,
        expires_at=datetime.fromtimestamp(expires, UTC),
    )


def require_site_service_request_ui_session(
    credentials: HTTPAuthorizationCredentials | None = Security(security),  # noqa: B008
) -> SiteServiceRequestUiSession:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="unauthorized")
    return verify_site_service_request_ui_session_token(credentials.credentials)
