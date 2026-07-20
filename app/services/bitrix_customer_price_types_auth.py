"""Bitrix embedded-app session auth for the customer price-type workplace.

Mirrors the executive-dashboard / procurement embedded-app pattern: after a
validated Bitrix launch (allowed domain + member_id + REST ``user.current``),
the Bitrix user is mapped to a read-only :class:`CustomerPriceTypeAccessScope`
and a short-lived signed session token is issued. The read API remains the
single source of role scope; this module only resolves *who* the user is and at
which scope. No 1C/Bitrix writes happen here.
"""

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
from typing import Any

from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials

from app.api.dependencies import security
from app.core.config import Settings, get_settings
from app.domains.customer_price_types import CustomerPriceTypeAccessScope

_TOKEN_ALG = "HS256"
_TOKEN_TYP = "MM-CUSTOMER-PRICE-TYPES"
_TOKEN_SCOPE = "customer_price_types"
_DOMAIN_RE = re.compile(r"^[a-z0-9.-]+$", re.IGNORECASE)
_BEARER_SECURITY = Security(security)

# Roles understood by the read API scope predicates. Money visibility defaults to
# full-access and finance only; everyone else sees no monetary fields.
_KNOWN_ROLES = (
    "network_head",
    "manager",
    "department_head",
    "master_data",
    "quality",
    "finance",
    "integration_operator",
)
_MONEY_ROLES = frozenset({"network_head", "finance"})


@dataclass(frozen=True)
class BitrixUser:
    user_id: str
    name: str | None
    department_ids: tuple[str, ...] = ()


def _b64_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def _as_string_list(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, list | tuple | set):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _normal_set(values: list[str]) -> set[str]:
    return {str(value).strip().lower() for value in values if str(value).strip()}


def _ensure_enabled(settings: Settings) -> None:
    if not settings.customer_price_type_bitrix_enabled:
        raise HTTPException(status_code=403, detail="Bitrix customer price-type app is disabled")
    if not settings.customer_price_type_bitrix_session_secret:
        raise HTTPException(
            status_code=500,
            detail="Bitrix customer price-type session secret is not configured",
        )


def normalize_bitrix_domain(value: str) -> str:
    raw = value.strip().lower()
    if "://" in raw:
        raw = urllib.parse.urlparse(raw).netloc
    raw = raw.split("/", 1)[0].strip()
    if not raw or not _DOMAIN_RE.fullmatch(raw):
        raise HTTPException(status_code=400, detail="invalid Bitrix domain")
    return raw


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
    if normalized_domain not in _normal_set(settings.customer_price_type_bitrix_allowed_domains):
        raise HTTPException(status_code=403, detail="Bitrix domain is not allowed")
    if normalized_member_id.lower() not in _normal_set(
        settings.customer_price_type_bitrix_allowed_member_ids
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
            timeout=settings.customer_price_type_bitrix_rest_timeout_seconds,
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
    name = (
        " ".join(
            part
            for part in (
                str(result.get("NAME") or "").strip(),
                str(result.get("LAST_NAME") or "").strip(),
            )
            if part
        )
        or None
    )
    department_ids = tuple(item for item in _as_string_list(result.get("UF_DEPARTMENT")) if item)
    return BitrixUser(user_id=str(result["ID"]).strip(), name=name, department_ids=department_ids)


def load_bitrix_headed_department_ids(
    *,
    domain: str,
    access_token: str,
    user_id: str,
    settings: Settings | None = None,
) -> tuple[str, ...]:
    """Return Bitrix department IDs headed by ``user_id`` (``department.get`` UF_HEAD).

    Lets the department_head role resolve by position instead of by a static user
    list. Failures degrade to an empty set so membership-based roles still resolve;
    requires the embedded app to hold the ``department`` REST scope.
    """
    settings = settings or get_settings()
    normalized_domain = normalize_bitrix_domain(domain)
    payload = json.dumps(
        {"auth": access_token, "FILTER": {"UF_HEAD": str(user_id).strip()}}
    ).encode("utf-8")
    request = urllib.request.Request(
        f"https://{normalized_domain}/rest/department.get.json",
        data=payload,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=settings.customer_price_type_bitrix_rest_timeout_seconds,
        ) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return ()
    if body.get("error"):
        return ()
    result = body.get("result")
    if not isinstance(result, list):
        return ()
    return tuple(
        str(item.get("ID")).strip()
        for item in result
        if isinstance(item, dict) and item.get("ID") not in (None, "")
    )


def _load_access_rules(settings: Settings) -> list[dict[str, Any]]:
    raw = settings.customer_price_type_access_rules_json
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=500, detail="Customer price-type access rules JSON is invalid"
        ) from exc
    items = payload.get("roles") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise HTTPException(
            status_code=500, detail="Customer price-type access rules must contain roles[]"
        )
    return [item for item in items if isinstance(item, dict)]


def _rule_department_ids(rule: dict[str, Any]) -> set[str]:
    return _normal_set(_as_string_list(rule.get("department_ids")))


def resolve_customer_price_type_access(
    *,
    bitrix_user_id: str,
    department_ids: tuple[str, ...] | list[str] = (),
    headed_department_ids: tuple[str, ...] | list[str] = (),
    settings: Settings | None = None,
) -> CustomerPriceTypeAccessScope:
    """Map a Bitrix user to a read-only scope by ORG POSITION, not by a user list.

    v1 grants access only to management roles: network head (member of a top
    management department), department head (heads a mapped Bitrix department),
    finance / master_data / quality (member of the matching department). A small
    break-glass full-access user list is still honoured for rollout / IT admins.
    Regular members without a management position get 403. Access follows Bitrix
    staffing automatically, so no per-person list needs maintaining.
    """
    settings = settings or get_settings()
    user_id = str(bitrix_user_id).strip()
    actor = f"bitrix:{user_id}"

    # Break-glass admin override (rollout / IT); the primary mechanism is position.
    if user_id.lower() in _normal_set(settings.customer_price_type_bitrix_full_access_user_ids):
        return CustomerPriceTypeAccessScope(actor=actor, role="network_head", can_view_money=True)

    member_depts = _normal_set(list(department_ids))
    headed_depts = _normal_set(list(headed_department_ids))
    rules = _load_access_rules(settings)

    # 1) Network head department -> full portfolio + money.
    for rule in rules:
        if str(
            rule.get("role") or ""
        ).strip() == "network_head" and member_depts & _rule_department_ids(rule):
            return CustomerPriceTypeAccessScope(
                actor=actor, role="network_head", can_view_money=True
            )

    # 2) Department head -> only their department(s); UF_HEAD mapped to 1C refs.
    for rule in rules:
        if str(rule.get("role") or "").strip() != "department_head":
            continue
        head_map = rule.get("head_department_refs")
        if not isinstance(head_map, dict):
            continue
        refs: set[str] = set()
        for bitrix_dept_id, onec_refs in head_map.items():
            if str(bitrix_dept_id).strip().lower() in headed_depts:
                refs.update(
                    item.strip().lower() for item in _as_string_list(onec_refs) if item.strip()
                )
        if refs:
            return CustomerPriceTypeAccessScope(
                actor=actor,
                role="department_head",
                department_refs=tuple(sorted(refs)),
                can_view_money=bool(rule.get("can_view_money", False)),
            )

    # 3) Functional management roles by department membership.
    for rule in rules:
        role = str(rule.get("role") or "").strip()
        if role in {"finance", "master_data", "quality"} and member_depts & _rule_department_ids(
            rule
        ):
            return CustomerPriceTypeAccessScope(
                actor=actor,
                role=role,
                can_view_money=bool(rule.get("can_view_money", role in _MONEY_ROLES)),
            )

    raise HTTPException(status_code=403, detail="Нет доступа к витрине типов цен")


def _secret(settings: Settings) -> bytes:
    secret = settings.customer_price_type_bitrix_session_secret
    if not secret:
        raise HTTPException(
            status_code=500,
            detail="Bitrix customer price-type session secret is not configured",
        )
    return secret.encode("utf-8")


def _sign(signing_input: str, *, settings: Settings) -> str:
    signature = hmac.new(_secret(settings), signing_input.encode("ascii"), hashlib.sha256).digest()
    return _b64_encode(signature)


def create_customer_price_type_session_token(
    *,
    domain: str,
    member_id: str,
    user_id: str,
    user_name: str | None,
    access: CustomerPriceTypeAccessScope,
    settings: Settings | None = None,
    now: int | None = None,
) -> tuple[str, int]:
    settings = settings or get_settings()
    _ensure_enabled(settings)
    issued_at = int(now if now is not None else time.time())
    expires_at_ts = issued_at + int(settings.customer_price_type_bitrix_session_ttl_seconds)
    payload: dict[str, Any] = {
        "sub": f"bitrix:{member_id}:{user_id}",
        "scope": _TOKEN_SCOPE,
        "domain": domain,
        "member_id": member_id,
        "user_id": user_id,
        "user_name": user_name,
        "role": access.role,
        "owner_ref": access.owner_ref,
        "department_refs": list(access.department_refs),
        "can_view_money": access.can_view_money,
        "iat": issued_at,
        "exp": expires_at_ts,
    }
    header = {"alg": _TOKEN_ALG, "typ": _TOKEN_TYP}
    header_raw = _b64_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_raw = _b64_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header_raw}.{payload_raw}"
    token = f"{signing_input}.{_sign(signing_input, settings=settings)}"
    return token, expires_at_ts


def verify_customer_price_type_session_token(
    token: str,
    *,
    settings: Settings | None = None,
    now: int | None = None,
) -> CustomerPriceTypeAccessScope:
    settings = settings or get_settings()
    _ensure_enabled(settings)
    try:
        header_raw, payload_raw, signature = token.split(".", 2)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="invalid customer price-type session") from exc

    signing_input = f"{header_raw}.{payload_raw}"
    if not hmac.compare_digest(signature, _sign(signing_input, settings=settings)):
        raise HTTPException(status_code=401, detail="invalid customer price-type session")
    try:
        header = json.loads(_b64_decode(header_raw).decode("utf-8"))
        payload = json.loads(_b64_decode(payload_raw).decode("utf-8"))
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=401, detail="invalid customer price-type session") from exc

    if header.get("alg") != _TOKEN_ALG or header.get("typ") != _TOKEN_TYP:
        raise HTTPException(status_code=401, detail="invalid customer price-type session")
    if payload.get("scope") != _TOKEN_SCOPE:
        raise HTTPException(status_code=401, detail="invalid customer price-type session")

    current_ts = int(now if now is not None else time.time())
    if int(payload.get("exp") or 0) <= current_ts:
        raise HTTPException(status_code=401, detail="customer price-type session expired")

    # Re-validate the launch allowlists on every request so revoking a domain or
    # member_id takes effect immediately, not only at session creation.
    domain, member_id = ensure_bitrix_launch_allowed(
        domain=str(payload.get("domain") or ""),
        member_id=str(payload.get("member_id") or ""),
        settings=settings,
    )
    user_id = str(payload.get("user_id") or "").strip()
    role = str(payload.get("role") or "").strip()
    if not user_id or role not in _KNOWN_ROLES:
        raise HTTPException(status_code=401, detail="invalid customer price-type session")
    return CustomerPriceTypeAccessScope(
        actor=f"bitrix:{member_id}:{user_id}",
        role=role,
        owner_ref=(str(payload["owner_ref"]).strip().lower() if payload.get("owner_ref") else None),
        department_refs=tuple(
            str(item).strip().lower() for item in _as_string_list(payload.get("department_refs"))
        ),
        can_view_money=bool(payload.get("can_view_money")),
    )


def verify_customer_price_type_session(
    credentials: HTTPAuthorizationCredentials | None = _BEARER_SECURITY,
) -> CustomerPriceTypeAccessScope:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="unauthorized")
    try:
        return verify_customer_price_type_session_token(credentials.credentials)
    except HTTPException as exc:
        if exc.status_code == 500:
            raise
        raise HTTPException(status_code=401, detail="unauthorized") from exc
