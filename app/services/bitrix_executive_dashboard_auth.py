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
from typing import Any, Literal

from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials

from app.api.dependencies import security
from app.core.config import Settings, get_settings

_TOKEN_ALG = "HS256"
_TOKEN_TYP = "MM-EXECUTIVE-DASHBOARD"
_TOKEN_SCOPE = "executive_dashboard"
_DOMAIN_RE = re.compile(r"^[a-z0-9.-]+$", re.IGNORECASE)
_BEARER_SECURITY = Security(security)
EXECUTIVE_DASHBOARD_BLOCK_KEYS = (
    "money_today",
    "profit_loss",
    "debtors",
    "receivables_control",
    "creditors_payables",
    "procurement_import",
    "warehouse_operations",
    "reconciliation",
    "tasks",
    "daily_focus",
)
EXECUTIVE_DASHBOARD_ACTION_DOMAINS = EXECUTIVE_DASHBOARD_BLOCK_KEYS
EXECUTIVE_DASHBOARD_MONEY_BLOCK_KEYS = (
    "money_today",
    "profit_loss",
    "debtors",
    "creditors_payables",
    "procurement_import",
)
_ROLE_DEFAULTS: dict[str, dict[str, Any]] = {
    "procurement": {
        "allowed_blocks": ("procurement_import",),
        "allowed_action_domains": ("procurement_import",),
        "money_blocks": ("procurement_import",),
        "personal_actions_only": False,
    },
    "receivables": {
        "allowed_blocks": ("debtors", "receivables_control"),
        "allowed_action_domains": ("debtors", "receivables_control"),
        "money_blocks": (),
        "personal_actions_only": False,
    },
    "finance": {
        "allowed_blocks": (
            "money_today",
            "profit_loss",
            "debtors",
            "receivables_control",
            "creditors_payables",
            "reconciliation",
        ),
        "allowed_action_domains": (
            "money_today",
            "profit_loss",
            "debtors",
            "receivables_control",
            "creditors_payables",
            "reconciliation",
        ),
        "money_blocks": (
            "money_today",
            "profit_loss",
            "debtors",
            "creditors_payables",
        ),
        "personal_actions_only": False,
    },
    "warehouse": {
        "allowed_blocks": ("warehouse_operations",),
        "allowed_action_domains": ("warehouse_operations",),
        "money_blocks": (),
        "personal_actions_only": False,
    },
    "personal": {
        "allowed_blocks": ("tasks", "daily_focus"),
        "allowed_action_domains": EXECUTIVE_DASHBOARD_ACTION_DOMAINS,
        "money_blocks": (),
        "personal_actions_only": True,
    },
}


@dataclass(frozen=True)
class BitrixUser:
    user_id: str
    name: str | None


@dataclass(frozen=True)
class ExecutiveDashboardAccess:
    access_level: Literal["full", "domain"]
    roles: tuple[str, ...]
    allowed_blocks: tuple[str, ...]
    allowed_action_domains: tuple[str, ...]
    money_blocks: tuple[str, ...]
    personal_actions_only: bool = False

    @property
    def is_full_access(self) -> bool:
        return self.access_level == "full"

    def allows_block(self, block_key: str) -> bool:
        return self.is_full_access or block_key in self.allowed_blocks

    def allows_action_domain(self, domain: str) -> bool:
        return self.is_full_access or domain in self.allowed_action_domains

    def can_view_money_block(self, block_key: str) -> bool:
        return self.is_full_access or block_key in self.money_blocks


@dataclass(frozen=True)
class ExecutiveDashboardSession:
    actor: str
    domain: str
    member_id: str
    user_id: str
    user_name: str | None
    access_level: Literal["full", "domain"]
    roles: tuple[str, ...]
    allowed_blocks: tuple[str, ...]
    allowed_action_domains: tuple[str, ...]
    money_blocks: tuple[str, ...]
    personal_actions_only: bool
    expires_at: datetime

    @property
    def is_full_access(self) -> bool:
        return self.access_level == "full"

    def allows_action_domain(self, domain: str) -> bool:
        return self.is_full_access or domain in self.allowed_action_domains

    def can_view_money_block(self, block_key: str) -> bool:
        return self.is_full_access or block_key in self.money_blocks


@dataclass(frozen=True)
class ExecutiveDashboardAuthContext:
    actor: str
    source: Literal["internal", "bitrix"]
    access_level: Literal["full", "domain"]
    bitrix_user_id: str | None = None
    roles: tuple[str, ...] = ()
    allowed_blocks: tuple[str, ...] = ()
    allowed_action_domains: tuple[str, ...] = ()
    money_blocks: tuple[str, ...] = ()
    personal_actions_only: bool = False

    @property
    def is_full_access(self) -> bool:
        return self.access_level == "full"

    def allows_block(self, block_key: str) -> bool:
        return self.is_full_access or block_key in self.allowed_blocks

    def allows_action_domain(self, domain: str) -> bool:
        return self.is_full_access or domain in self.allowed_action_domains

    def can_view_money_block(self, block_key: str) -> bool:
        return self.is_full_access or block_key in self.money_blocks


def _ordered_values(
    values: list[str], *, known_order: tuple[str, ...] | None = None
) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value or "").strip()
        if not item or item in seen:
            continue
        normalized.append(item)
        seen.add(item)
    if known_order is None:
        return tuple(normalized)
    rank = {item: index for index, item in enumerate(known_order)}
    normalized.sort(key=lambda item: (rank.get(item, len(rank)), item))
    return tuple(normalized)


def _as_string_list(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, list | tuple | set):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _full_access() -> ExecutiveDashboardAccess:
    return ExecutiveDashboardAccess(
        access_level="full",
        roles=("full",),
        allowed_blocks=EXECUTIVE_DASHBOARD_BLOCK_KEYS,
        allowed_action_domains=EXECUTIVE_DASHBOARD_ACTION_DOMAINS,
        money_blocks=EXECUTIVE_DASHBOARD_MONEY_BLOCK_KEYS,
        personal_actions_only=False,
    )


def _legacy_domain_access() -> ExecutiveDashboardAccess:
    return ExecutiveDashboardAccess(
        access_level="domain",
        roles=("legacy_domain",),
        allowed_blocks=EXECUTIVE_DASHBOARD_BLOCK_KEYS,
        allowed_action_domains=EXECUTIVE_DASHBOARD_ACTION_DOMAINS,
        money_blocks=(),
        personal_actions_only=True,
    )


def full_executive_dashboard_context() -> ExecutiveDashboardAuthContext:
    access = _full_access()
    return ExecutiveDashboardAuthContext(
        actor="internal:management",
        source="internal",
        access_level=access.access_level,
        bitrix_user_id=None,
        roles=access.roles,
        allowed_blocks=access.allowed_blocks,
        allowed_action_domains=access.allowed_action_domains,
        money_blocks=access.money_blocks,
        personal_actions_only=access.personal_actions_only,
    )


def legacy_domain_executive_dashboard_context(
    bitrix_user_id: str | None,
) -> ExecutiveDashboardAuthContext:
    access = _legacy_domain_access()
    return ExecutiveDashboardAuthContext(
        actor=f"bitrix:legacy:{bitrix_user_id or ''}",
        source="bitrix",
        access_level=access.access_level,
        bitrix_user_id=bitrix_user_id,
        roles=access.roles,
        allowed_blocks=access.allowed_blocks,
        allowed_action_domains=access.allowed_action_domains,
        money_blocks=access.money_blocks,
        personal_actions_only=access.personal_actions_only,
    )


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


def _normalize_ref(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _ensure_enabled(settings: Settings) -> None:
    if not settings.executive_dashboard_bitrix_enabled:
        raise HTTPException(status_code=403, detail="Bitrix executive dashboard app is disabled")
    if not settings.executive_dashboard_bitrix_session_secret:
        raise HTTPException(
            status_code=500,
            detail="Bitrix executive dashboard session secret is not configured",
        )


def _load_access_rule_items(settings: Settings) -> list[dict[str, Any]]:
    raw = settings.executive_dashboard_access_rules_json
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=500,
            detail="Executive dashboard access rules JSON is invalid",
        ) from exc
    items = payload.get("roles") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise HTTPException(
            status_code=500,
            detail="Executive dashboard access rules must contain roles[]",
        )
    rules: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict):
            rules.append(item)
    return rules


def _rule_list(
    rule: dict[str, Any],
    key: str,
    *,
    fallback: tuple[str, ...] = (),
) -> list[str]:
    values = _as_string_list(rule.get(key))
    return values if values else list(fallback)


def _access_from_rules(rules: list[dict[str, Any]]) -> ExecutiveDashboardAccess | None:
    if not rules:
        return None
    roles: list[str] = []
    allowed_blocks: list[str] = []
    allowed_action_domains: list[str] = []
    money_blocks: list[str] = []
    personal_flags: list[bool] = []

    for rule in rules:
        role = str(rule.get("role") or "").strip()
        if not role:
            continue
        role_defaults = _ROLE_DEFAULTS.get(role, {})
        if role == "full":
            return _full_access()
        roles.append(role)
        allowed_blocks.extend(
            _rule_list(
                rule,
                "allowed_blocks",
                fallback=tuple(role_defaults.get("allowed_blocks") or ()),
            )
        )
        allowed_action_domains.extend(
            _rule_list(
                rule,
                "allowed_action_domains",
                fallback=tuple(role_defaults.get("allowed_action_domains") or ()),
            )
        )
        money_blocks.extend(
            _rule_list(
                rule,
                "money_blocks",
                fallback=tuple(role_defaults.get("money_blocks") or ()),
            )
        )
        personal_flags.append(
            bool(
                rule.get("personal_actions_only", role_defaults.get("personal_actions_only", False))
            )
        )

    if not roles:
        return None
    return ExecutiveDashboardAccess(
        access_level="domain",
        roles=_ordered_values(roles),
        allowed_blocks=_ordered_values(
            allowed_blocks,
            known_order=EXECUTIVE_DASHBOARD_BLOCK_KEYS,
        ),
        allowed_action_domains=_ordered_values(
            allowed_action_domains,
            known_order=EXECUTIVE_DASHBOARD_ACTION_DOMAINS,
        ),
        money_blocks=_ordered_values(
            money_blocks,
            known_order=EXECUTIVE_DASHBOARD_MONEY_BLOCK_KEYS,
        ),
        personal_actions_only=bool(personal_flags) and all(personal_flags),
    )


def _resolve_rule_access(
    *,
    bitrix_user_id: str,
    settings: Settings,
) -> ExecutiveDashboardAccess | None:
    user_id = bitrix_user_id.strip().lower()
    matched_rules = [
        rule
        for rule in _load_access_rule_items(settings)
        if user_id in _normal_set(_as_string_list(rule.get("bitrix_user_ids")))
    ]
    return _access_from_rules(matched_rules)


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
    if normalized_domain not in _normal_set(settings.executive_dashboard_bitrix_allowed_domains):
        raise HTTPException(status_code=403, detail="Bitrix domain is not allowed")
    if normalized_member_id.lower() not in _normal_set(
        settings.executive_dashboard_bitrix_allowed_member_ids
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
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=settings.executive_dashboard_bitrix_rest_timeout_seconds,
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


def resolve_executive_dashboard_access(
    *,
    bitrix_user_id: str,
    settings: Settings | None = None,
) -> ExecutiveDashboardAccess:
    settings = settings or get_settings()
    user_id = str(bitrix_user_id).strip()
    if user_id.lower() in _normal_set(settings.executive_dashboard_bitrix_full_access_user_ids):
        return _full_access()
    rule_access = _resolve_rule_access(bitrix_user_id=user_id, settings=settings)
    if rule_access is not None:
        return rule_access
    if user_id.lower() in _normal_set(settings.executive_dashboard_bitrix_domain_access_user_ids):
        return _legacy_domain_access()
    raise HTTPException(status_code=403, detail="Нет доступа к управленческой витрине")


def _b64_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def _secret(settings: Settings) -> bytes:
    secret = settings.executive_dashboard_bitrix_session_secret
    if not secret:
        raise HTTPException(
            status_code=500,
            detail="Bitrix executive dashboard session secret is not configured",
        )
    return secret.encode("utf-8")


def _sign(signing_input: str, *, settings: Settings) -> str:
    signature = hmac.new(_secret(settings), signing_input.encode("ascii"), hashlib.sha256).digest()
    return _b64_encode(signature)


def create_executive_dashboard_session_token(
    *,
    domain: str,
    member_id: str,
    user_id: str,
    user_name: str | None,
    access: ExecutiveDashboardAccess,
    settings: Settings | None = None,
    now: int | None = None,
) -> tuple[str, datetime]:
    settings = settings or get_settings()
    _ensure_enabled(settings)
    issued_at = int(now if now is not None else time.time())
    expires_at_ts = issued_at + int(settings.executive_dashboard_bitrix_session_ttl_seconds)
    payload: dict[str, Any] = {
        "sub": f"bitrix:{member_id}:{user_id}",
        "scope": _TOKEN_SCOPE,
        "domain": domain,
        "member_id": member_id,
        "user_id": user_id,
        "user_name": user_name,
        "access_level": access.access_level,
        "roles": list(access.roles),
        "allowed_blocks": list(access.allowed_blocks),
        "allowed_action_domains": list(access.allowed_action_domains),
        "money_blocks": list(access.money_blocks),
        "personal_actions_only": access.personal_actions_only,
        "iat": issued_at,
        "exp": expires_at_ts,
    }
    header = {"alg": _TOKEN_ALG, "typ": _TOKEN_TYP}
    header_raw = _b64_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_raw = _b64_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header_raw}.{payload_raw}"
    token = f"{signing_input}.{_sign(signing_input, settings=settings)}"
    return token, datetime.fromtimestamp(expires_at_ts, UTC)


def verify_executive_dashboard_session_token(
    token: str,
    *,
    settings: Settings | None = None,
    now: int | None = None,
) -> ExecutiveDashboardSession:
    settings = settings or get_settings()
    _ensure_enabled(settings)
    try:
        header_raw, payload_raw, signature = token.split(".", 2)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="invalid executive dashboard session") from exc

    signing_input = f"{header_raw}.{payload_raw}"
    expected_signature = _sign(signing_input, settings=settings)
    if not hmac.compare_digest(signature, expected_signature):
        raise HTTPException(status_code=401, detail="invalid executive dashboard session")

    try:
        header = json.loads(_b64_decode(header_raw).decode("utf-8"))
        payload = json.loads(_b64_decode(payload_raw).decode("utf-8"))
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=401, detail="invalid executive dashboard session") from exc

    if header.get("alg") != _TOKEN_ALG or header.get("typ") != _TOKEN_TYP:
        raise HTTPException(status_code=401, detail="invalid executive dashboard session")
    if payload.get("scope") != _TOKEN_SCOPE:
        raise HTTPException(status_code=401, detail="invalid executive dashboard session")

    current_ts = int(now if now is not None else time.time())
    expires_at_ts = int(payload.get("exp") or 0)
    if expires_at_ts <= current_ts:
        raise HTTPException(status_code=401, detail="executive dashboard session expired")

    domain, member_id = ensure_bitrix_launch_allowed(
        domain=str(payload.get("domain") or ""),
        member_id=str(payload.get("member_id") or ""),
        settings=settings,
    )
    user_id = str(payload.get("user_id") or "").strip()
    if not user_id:
        raise HTTPException(status_code=401, detail="invalid executive dashboard session")
    access_level = str(payload.get("access_level") or "")
    if access_level not in {"full", "domain"}:
        raise HTTPException(status_code=401, detail="invalid executive dashboard session")
    if access_level == "full":
        access = _full_access()
    elif "roles" in payload or "allowed_blocks" in payload:
        access = ExecutiveDashboardAccess(
            access_level="domain",
            roles=_ordered_values(_as_string_list(payload.get("roles"))),
            allowed_blocks=_ordered_values(
                _as_string_list(payload.get("allowed_blocks")),
                known_order=EXECUTIVE_DASHBOARD_BLOCK_KEYS,
            ),
            allowed_action_domains=_ordered_values(
                _as_string_list(payload.get("allowed_action_domains")),
                known_order=EXECUTIVE_DASHBOARD_ACTION_DOMAINS,
            ),
            money_blocks=_ordered_values(
                _as_string_list(payload.get("money_blocks")),
                known_order=EXECUTIVE_DASHBOARD_MONEY_BLOCK_KEYS,
            ),
            personal_actions_only=bool(payload.get("personal_actions_only")),
        )
    else:
        access = _legacy_domain_access()

    return ExecutiveDashboardSession(
        actor=f"bitrix:{member_id}:{user_id}",
        domain=domain,
        member_id=member_id,
        user_id=user_id,
        user_name=_normalize_ref(payload.get("user_name")),
        access_level=access.access_level,
        roles=access.roles,
        allowed_blocks=access.allowed_blocks,
        allowed_action_domains=access.allowed_action_domains,
        money_blocks=access.money_blocks,
        personal_actions_only=access.personal_actions_only,
        expires_at=datetime.fromtimestamp(expires_at_ts, UTC),
    )


def _management_internal_token() -> str | None:
    settings = get_settings()
    return (
        settings.management_internal_api_token
        or settings.counterparty_duplicate_internal_api_token
        or settings.return_scheme_internal_api_token
    )


def require_executive_dashboard_access(
    credentials: HTTPAuthorizationCredentials | None = _BEARER_SECURITY,
) -> ExecutiveDashboardAuthContext:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="unauthorized")

    token = credentials.credentials
    internal_token = _management_internal_token()
    if internal_token and token == internal_token:
        return full_executive_dashboard_context()

    try:
        session = verify_executive_dashboard_session_token(token)
    except HTTPException as exc:
        if exc.status_code == 500:
            raise
        raise HTTPException(status_code=401, detail="unauthorized") from exc
    return ExecutiveDashboardAuthContext(
        actor=session.actor,
        source="bitrix",
        access_level=session.access_level,
        bitrix_user_id=session.user_id,
        roles=session.roles,
        allowed_blocks=session.allowed_blocks,
        allowed_action_domains=session.allowed_action_domains,
        money_blocks=session.money_blocks,
        personal_actions_only=session.personal_actions_only,
    )
