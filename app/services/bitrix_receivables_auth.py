from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models import (
    ReceivableBitrixUserAccess,
    ReceivableCase,
    StaffMember,
    TelephonyUserLineSnapshot,
)
from app.services.receivable_department_aliases import (
    expand_receivable_department_refs,
    receivable_department_names_equivalent,
)

logger = logging.getLogger(__name__)

_TOKEN_ALG = "HS256"
_TOKEN_TYP = "MM-RECEIVABLES"
_TOKEN_SCOPE = "receivables"
_DOMAIN_RE = re.compile(r"^[a-z0-9.-]+$", re.IGNORECASE)
_DEPARTMENT_ACCESS_NOT_FOUND_DETAIL = (
    "Не найдено подразделение для доступа: проверьте привязку пользователя к подразделению"
)
_BITRIX_COURIER_POSITION_MARKERS = ("курьер", "courier", "kurer")


@dataclass(frozen=True)
class BitrixUser:
    user_id: str
    name: str | None
    active: bool | None = None
    work_position: str | None = None
    department_ids: tuple[str, ...] = ()
    department_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReceivablesAccess:
    access_level: Literal["full", "department"]
    department_refs: frozenset[str]

    @property
    def is_full_access(self) -> bool:
        return self.access_level == "full"


@dataclass(frozen=True)
class ReceivablesSession:
    actor: str
    domain: str
    member_id: str
    user_id: str
    user_name: str | None
    access_level: Literal["full", "department"]
    department_refs: frozenset[str]
    expires_at: datetime

    @property
    def is_full_access(self) -> bool:
        return self.access_level == "full"


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


def _normalize_name(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().replace("ё", "е").split())


def _normalize_department_ids(value: Any) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    raw_values = value if isinstance(value, (list, tuple, set)) else [value]
    department_ids: list[str] = []
    seen: set[str] = set()
    for raw_value in raw_values:
        normalized = str(raw_value or "").strip()
        if not normalized or normalized in seen:
            continue
        department_ids.append(normalized)
        seen.add(normalized)
    return tuple(department_ids)


def _parse_bitrix_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().casefold()
    if normalized in {"true", "y", "yes", "1"}:
        return True
    if normalized in {"false", "n", "no", "0"}:
        return False
    return None


def _is_bitrix_courier_position(value: str | None) -> bool:
    normalized = _normalize_name(value)
    return any(marker in normalized for marker in _BITRIX_COURIER_POSITION_MARKERS)


def _ensure_bitrix_user_eligible_for_receivables(bitrix_user: BitrixUser | None) -> None:
    if bitrix_user is None:
        return
    if bitrix_user.active is False:
        raise HTTPException(
            status_code=403,
            detail="Доступ к рабочему месту дебиторки закрыт для неактивных сотрудников",
        )
    if _is_bitrix_courier_position(bitrix_user.work_position):
        raise HTTPException(
            status_code=403,
            detail="Доступ к рабочему месту дебиторки закрыт для курьеров",
        )


def _ensure_bitrix_enabled(settings: Settings) -> None:
    if not settings.receivable_workplace_bitrix_enabled:
        raise HTTPException(status_code=403, detail="Bitrix receivables app is disabled")
    if not settings.receivable_workplace_bitrix_session_secret:
        raise HTTPException(
            status_code=500,
            detail="Bitrix receivables session secret is not configured",
        )


def ensure_bitrix_launch_allowed(
    *,
    domain: str,
    member_id: str,
    settings: Settings | None = None,
) -> tuple[str, str]:
    settings = settings or get_settings()
    _ensure_bitrix_enabled(settings)
    normalized_domain = normalize_bitrix_domain(domain)
    normalized_member_id = member_id.strip()
    if normalized_domain not in _normal_set(settings.receivable_workplace_bitrix_allowed_domains):
        raise HTTPException(status_code=403, detail="Bitrix domain is not allowed")
    if normalized_member_id.lower() not in _normal_set(
        settings.receivable_workplace_bitrix_allowed_member_ids
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
    body = _call_bitrix_rest(
        domain=normalized_domain,
        access_token=access_token,
        method="user.current",
        settings=settings,
        auth_error_status=401,
        auth_error_detail="Bitrix access token was rejected",
        unavailable_detail="Bitrix user.current is unavailable",
    )

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
    work_position = str(result.get("WORK_POSITION") or "").strip() or None
    active = _parse_bitrix_bool(result.get("ACTIVE"))
    department_ids = _normalize_department_ids(result.get("UF_DEPARTMENT"))
    department_names = ()
    if active is not False and not _is_bitrix_courier_position(work_position):
        department_names = _load_bitrix_department_names(
            domain=normalized_domain,
            access_token=access_token,
            department_ids=department_ids,
            settings=settings,
        )
    return BitrixUser(
        user_id=str(result["ID"]).strip(),
        name=name,
        active=active,
        work_position=work_position,
        department_ids=department_ids,
        department_names=department_names,
    )


def _call_bitrix_rest(
    *,
    domain: str,
    access_token: str,
    method: str,
    settings: Settings,
    auth_error_detail: str,
    unavailable_detail: str,
    payload: dict[str, Any] | None = None,
    auth_error_status: int = 502,
) -> dict[str, Any]:
    request_payload = {"auth": access_token, **(payload or {})}
    request = urllib.request.Request(
        f"https://{domain}/rest/{method}.json",
        data=json.dumps(request_payload).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=settings.receivable_workplace_bitrix_rest_timeout_seconds,
        ) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise HTTPException(status_code=auth_error_status, detail=auth_error_detail) from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=502, detail=unavailable_detail) from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=502, detail=unavailable_detail)
    return body


def _call_bitrix_webhook_rest(
    *,
    base_url: str,
    method: str,
    settings: Settings,
    unavailable_detail: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/{method}.json",
        data=json.dumps(payload or {}).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=settings.receivable_workplace_bitrix_rest_timeout_seconds,
        ) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        TimeoutError,
        json.JSONDecodeError,
    ) as exc:
        raise HTTPException(status_code=502, detail=unavailable_detail) from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=502, detail=unavailable_detail)
    return body


def _department_rows_from_bitrix_body(body: dict[str, Any]) -> list[dict[str, Any]]:
    if body.get("error"):
        return []
    result = body.get("result")
    department_rows = [result] if isinstance(result, dict) else result
    if not isinstance(department_rows, list):
        return []
    return [item for item in department_rows if isinstance(item, dict)]


def _department_webhook_base(settings: Settings) -> str | None:
    base_url = settings.receivable_bitrix_webhook_url or settings.bitrix_box_webhook_base
    normalized = str(base_url or "").strip()
    return normalized or None


def _load_bitrix_department_rows(
    *,
    domain: str,
    access_token: str,
    department_id: str,
    settings: Settings,
) -> list[dict[str, Any]]:
    launch_error: HTTPException | None = None
    try:
        launch_body = _call_bitrix_rest(
            domain=domain,
            access_token=access_token,
            method="department.get",
            settings=settings,
            payload={"ID": department_id},
            auth_error_detail="Bitrix department.get is unavailable",
            unavailable_detail="Bitrix department.get is unavailable",
        )
    except HTTPException as exc:
        launch_error = exc
    else:
        launch_rows = _department_rows_from_bitrix_body(launch_body)
        if launch_rows:
            return launch_rows

    webhook_base = _department_webhook_base(settings)
    if webhook_base:
        webhook_body = _call_bitrix_webhook_rest(
            base_url=webhook_base,
            method="department.get",
            settings=settings,
            payload={"ID": department_id},
            unavailable_detail="Bitrix department.get is unavailable",
        )
        webhook_rows = _department_rows_from_bitrix_body(webhook_body)
        if webhook_rows:
            return webhook_rows

    if launch_error is not None:
        raise launch_error
    return []


def _load_bitrix_department_names(
    *,
    domain: str,
    access_token: str,
    department_ids: tuple[str, ...],
    settings: Settings,
) -> tuple[str, ...]:
    department_names: list[str] = []
    seen: set[str] = set()
    for department_id in department_ids:
        department_rows = _load_bitrix_department_rows(
            domain=domain,
            access_token=access_token,
            department_id=department_id,
            settings=settings,
        )
        for item in department_rows:
            department_name = str(item.get("NAME") or item.get("name") or "").strip()
            if not department_name or department_name in seen:
                continue
            department_names.append(department_name)
            seen.add(department_name)
    return tuple(department_names)


def _full_access_user_ids(settings: Settings) -> set[str]:
    return _normal_set(settings.receivable_workplace_bitrix_full_access_user_ids)


def _table_access_for_user(
    session: Session,
    *,
    bitrix_user_id: str,
) -> ReceivablesAccess | None:
    row = session.scalar(
        select(ReceivableBitrixUserAccess).where(
            ReceivableBitrixUserAccess.bitrix_user_id == bitrix_user_id,
            ReceivableBitrixUserAccess.is_active.is_(True),
        )
    )
    if row is None:
        return None
    if row.access_level == "full":
        return ReceivablesAccess(access_level="full", department_refs=frozenset())
    if row.access_level != "department":
        logger.warning(
            "receivables_access_table_invalid_level",
            extra={"bitrix_user_id": bitrix_user_id, "access_level": row.access_level},
        )
        return None
    department_refs = expand_receivable_department_refs(
        value for value in (_normalize_ref(item) for item in row.department_refs or []) if value
    )
    if not department_refs:
        logger.info(
            "receivables_access_table_department_empty",
            extra={
                "bitrix_user_id": bitrix_user_id,
                "access_source": "table",
                "reason": "department_refs_empty",
            },
        )
        raise HTTPException(
            status_code=403,
            detail="Не найдено подразделение для доступа: проверьте привязку пользователя к подразделению",
        )
    return ReceivablesAccess(access_level="department", department_refs=department_refs)


def _resolve_department_refs_by_names(
    session: Session,
    *,
    names: set[str],
) -> set[str]:
    if not names:
        return set()

    def matches(candidate: str | None) -> bool:
        return any(receivable_department_names_equivalent(candidate, name) for name in names)

    refs: set[str] = set()
    latest_case_date = session.scalar(select(func.max(ReceivableCase.snapshot_date)))
    if latest_case_date is not None:
        case_rows = session.execute(
            select(ReceivableCase.department_ref, ReceivableCase.department_name).where(
                ReceivableCase.snapshot_date == latest_case_date,
                ReceivableCase.department_ref.is_not(None),
                ReceivableCase.department_name.is_not(None),
            )
        ).all()
        for department_ref, department_name in case_rows:
            if matches(department_name) and department_ref:
                refs.add(str(department_ref))

    staff_rows = session.execute(
        select(
            StaffMember.department_ref,
            StaffMember.department_name,
            StaffMember.store_ref,
            StaffMember.store_name,
        ).where(StaffMember.employment_status != "fired")
    ).all()
    for department_ref, department_name, store_ref, store_name in staff_rows:
        if department_ref and matches(department_name):
            refs.add(str(department_ref))
        if store_ref and matches(store_name):
            refs.add(str(store_ref))
    return refs


def _resolve_bitrix_profile_access(
    session: Session,
    *,
    bitrix_user: BitrixUser | None,
) -> ReceivablesAccess | None:
    if bitrix_user is None:
        return None
    department_names = {
        value for value in (_normalize_name(name) for name in bitrix_user.department_names) if value
    }
    if not department_names:
        return None
    fallback_refs = _resolve_department_refs_by_names(session, names=department_names)
    department_refs = expand_receivable_department_refs(fallback_refs, names=department_names)
    if not department_refs:
        logger.info(
            "receivables_access_bitrix_profile_department_not_found",
            extra={
                "bitrix_user_id": bitrix_user.user_id,
                "access_source": "bitrix_profile",
                "reason": "department_not_found",
                "department_ids": list(bitrix_user.department_ids),
                "department_names": sorted(department_names),
                "fallback_department_refs": sorted(fallback_refs),
            },
        )
        return None
    logger.info(
        "receivables_access_resolved_from_bitrix_profile",
        extra={
            "bitrix_user_id": bitrix_user.user_id,
            "department_ref_count": len(department_refs),
            "access_source": "bitrix_profile",
            "reason": "resolved",
        },
    )
    return ReceivablesAccess(access_level="department", department_refs=department_refs)


def resolve_receivables_access(
    session: Session,
    *,
    bitrix_user_id: str,
    bitrix_user: BitrixUser | None = None,
    settings: Settings | None = None,
) -> ReceivablesAccess:
    settings = settings or get_settings()
    user_id = str(bitrix_user_id).strip()
    _ensure_bitrix_user_eligible_for_receivables(bitrix_user)
    table_access = _table_access_for_user(session, bitrix_user_id=user_id)
    if table_access is not None:
        logger.info(
            "receivables_access_resolved_from_table",
            extra={
                "bitrix_user_id": user_id,
                "access_level": table_access.access_level,
                "department_ref_count": len(table_access.department_refs),
            },
        )
        return table_access
    if user_id.lower() in _full_access_user_ids(settings):
        return ReceivablesAccess(access_level="full", department_refs=frozenset())

    bitrix_profile_access = _resolve_bitrix_profile_access(session, bitrix_user=bitrix_user)

    active_user_predicate = or_(
        TelephonyUserLineSnapshot.employment_status.is_(None),
        TelephonyUserLineSnapshot.employment_status != "fired",
    )
    latest_snapshot_date = session.scalar(
        select(func.max(TelephonyUserLineSnapshot.snapshot_date)).where(
            TelephonyUserLineSnapshot.bitrix_user_id == user_id,
            TelephonyUserLineSnapshot.is_marked.is_(False),
            active_user_predicate,
        )
    )
    if latest_snapshot_date is None:
        if bitrix_profile_access is not None:
            return bitrix_profile_access
        logger.info(
            "receivables_access_snapshot_not_found",
            extra={
                "bitrix_user_id": user_id,
                "access_source": "telephony",
                "reason": "snapshot_not_found",
                "raw_department_refs": [],
                "fallback_department_refs": [],
            },
        )
        raise HTTPException(
            status_code=403,
            detail=_DEPARTMENT_ACCESS_NOT_FOUND_DETAIL,
        )

    rows = (
        session.execute(
            select(TelephonyUserLineSnapshot).where(
                TelephonyUserLineSnapshot.snapshot_date == latest_snapshot_date,
                TelephonyUserLineSnapshot.bitrix_user_id == user_id,
                TelephonyUserLineSnapshot.is_marked.is_(False),
                active_user_predicate,
            )
        )
        .scalars()
        .all()
    )
    raw_refs = {
        value
        for row in rows
        for value in (
            _normalize_ref(row.staff_department_ref),
            _normalize_ref(row.staff_store_ref),
            _normalize_ref(row.department_ref_hex),
            _normalize_ref(row.store_ref_hex),
        )
        if value
    }
    department_names = {
        value
        for row in rows
        for value in (
            _normalize_name(row.staff_department_name),
            _normalize_name(row.staff_store_name),
            _normalize_name(row.department_name),
            _normalize_name(row.store_name),
        )
        if value
    }
    fallback_refs = _resolve_department_refs_by_names(session, names=department_names)
    department_refs = expand_receivable_department_refs(
        raw_refs | fallback_refs, names=department_names
    )
    if not department_refs:
        if bitrix_profile_access is not None:
            return bitrix_profile_access
        logger.info(
            "receivables_access_department_not_found",
            extra={
                "bitrix_user_id": user_id,
                "access_source": "telephony",
                "reason": "department_not_found",
                "telephony_rows": len(rows),
                "department_names": sorted(department_names),
                "raw_department_refs": sorted(raw_refs),
                "fallback_department_refs": sorted(fallback_refs),
            },
        )
        raise HTTPException(
            status_code=403,
            detail=_DEPARTMENT_ACCESS_NOT_FOUND_DETAIL,
        )
    logger.info(
        "receivables_access_resolved",
        extra={
            "bitrix_user_id": user_id,
            "raw_ref_count": len(raw_refs),
            "fallback_ref_count": len(fallback_refs),
            "department_ref_count": len(department_refs),
            "access_source": "telephony",
            "reason": "resolved",
        },
    )
    return ReceivablesAccess(access_level="department", department_refs=department_refs)


def diagnose_receivables_access(
    session: Session,
    *,
    bitrix_user_id: str,
    settings: Settings | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    user_id = str(bitrix_user_id).strip()
    table_row = session.scalar(
        select(ReceivableBitrixUserAccess).where(
            ReceivableBitrixUserAccess.bitrix_user_id == user_id,
        )
    )
    table_payload = None
    if table_row is not None:
        table_payload = {
            "access_level": table_row.access_level,
            "department_refs": list(table_row.department_refs or []),
            "is_active": table_row.is_active,
        }

    active_user_predicate = or_(
        TelephonyUserLineSnapshot.employment_status.is_(None),
        TelephonyUserLineSnapshot.employment_status != "fired",
    )
    latest_snapshot_date = session.scalar(
        select(func.max(TelephonyUserLineSnapshot.snapshot_date)).where(
            TelephonyUserLineSnapshot.bitrix_user_id == user_id,
            TelephonyUserLineSnapshot.is_marked.is_(False),
            active_user_predicate,
        )
    )
    rows = []
    if latest_snapshot_date is not None:
        rows = (
            session.execute(
                select(TelephonyUserLineSnapshot).where(
                    TelephonyUserLineSnapshot.snapshot_date == latest_snapshot_date,
                    TelephonyUserLineSnapshot.bitrix_user_id == user_id,
                    TelephonyUserLineSnapshot.is_marked.is_(False),
                    active_user_predicate,
                )
            )
            .scalars()
            .all()
        )
    raw_refs = {
        value
        for row in rows
        for value in (
            _normalize_ref(row.staff_department_ref),
            _normalize_ref(row.staff_store_ref),
            _normalize_ref(row.department_ref_hex),
            _normalize_ref(row.store_ref_hex),
        )
        if value
    }
    department_names = {
        value
        for row in rows
        for value in (
            _normalize_name(row.staff_department_name),
            _normalize_name(row.staff_store_name),
            _normalize_name(row.department_name),
            _normalize_name(row.store_name),
        )
        if value
    }
    fallback_refs = _resolve_department_refs_by_names(session, names=department_names)
    resolved_refs = sorted(
        expand_receivable_department_refs(raw_refs | fallback_refs, names=department_names)
    )
    reason = "resolved"
    if table_row is not None and table_row.is_active:
        reason = "table"
    elif user_id.lower() in _full_access_user_ids(settings):
        reason = "env_full_access"
    elif not latest_snapshot_date:
        reason = "telephony_snapshot_not_found"
    elif not resolved_refs:
        reason = "department_not_found"
    return {
        "bitrix_user_id": user_id,
        "table_access": table_payload,
        "env_full_access": user_id.lower() in _full_access_user_ids(settings),
        "latest_telephony_snapshot_date": latest_snapshot_date,
        "telephony_rows": len(rows),
        "raw_department_refs": sorted(raw_refs),
        "fallback_department_refs": sorted(fallback_refs),
        "resolved_department_refs": resolved_refs,
        "reason": reason,
    }


def _b64_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def _secret(settings: Settings) -> bytes:
    secret = settings.receivable_workplace_bitrix_session_secret
    if not secret:
        raise HTTPException(
            status_code=500,
            detail="Bitrix receivables session secret is not configured",
        )
    return secret.encode("utf-8")


def _sign(signing_input: str, *, settings: Settings) -> str:
    signature = hmac.new(_secret(settings), signing_input.encode("ascii"), hashlib.sha256).digest()
    return _b64_encode(signature)


def create_receivables_session_token(
    *,
    domain: str,
    member_id: str,
    user_id: str,
    user_name: str | None,
    access: ReceivablesAccess,
    settings: Settings | None = None,
    now: int | None = None,
) -> tuple[str, datetime]:
    settings = settings or get_settings()
    _ensure_bitrix_enabled(settings)
    issued_at = int(now if now is not None else time.time())
    expires_at_ts = issued_at + int(settings.receivable_workplace_bitrix_session_ttl_seconds)
    payload: dict[str, Any] = {
        "sub": f"bitrix:{member_id}:{user_id}",
        "scope": _TOKEN_SCOPE,
        "domain": domain,
        "member_id": member_id,
        "user_id": user_id,
        "user_name": user_name,
        "access_level": access.access_level,
        "department_refs": sorted(
            expand_receivable_department_refs(access.department_refs)
            if access.access_level == "department"
            else access.department_refs
        ),
        "iat": issued_at,
        "exp": expires_at_ts,
    }
    header = {"alg": _TOKEN_ALG, "typ": _TOKEN_TYP}
    header_raw = _b64_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_raw = _b64_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header_raw}.{payload_raw}"
    token = f"{signing_input}.{_sign(signing_input, settings=settings)}"
    return token, datetime.fromtimestamp(expires_at_ts, UTC)


def verify_receivables_session_token(
    token: str,
    *,
    settings: Settings | None = None,
    now: int | None = None,
) -> ReceivablesSession:
    settings = settings or get_settings()
    _ensure_bitrix_enabled(settings)
    try:
        header_raw, payload_raw, signature = token.split(".", 2)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="invalid receivables session") from exc

    signing_input = f"{header_raw}.{payload_raw}"
    expected_signature = _sign(signing_input, settings=settings)
    if not hmac.compare_digest(signature, expected_signature):
        raise HTTPException(status_code=401, detail="invalid receivables session")

    try:
        header = json.loads(_b64_decode(header_raw).decode("utf-8"))
        payload = json.loads(_b64_decode(payload_raw).decode("utf-8"))
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=401, detail="invalid receivables session") from exc

    if header.get("alg") != _TOKEN_ALG or header.get("typ") != _TOKEN_TYP:
        raise HTTPException(status_code=401, detail="invalid receivables session")
    if payload.get("scope") != _TOKEN_SCOPE:
        raise HTTPException(status_code=401, detail="invalid receivables session")

    current_ts = int(now if now is not None else time.time())
    expires_at_ts = int(payload.get("exp") or 0)
    if expires_at_ts <= current_ts:
        raise HTTPException(status_code=401, detail="receivables session expired")

    domain, member_id = ensure_bitrix_launch_allowed(
        domain=str(payload.get("domain") or ""),
        member_id=str(payload.get("member_id") or ""),
        settings=settings,
    )
    user_id = str(payload.get("user_id") or "").strip()
    if not user_id:
        raise HTTPException(status_code=401, detail="invalid receivables session")

    access_level = str(payload.get("access_level") or "")
    if access_level not in {"full", "department"}:
        raise HTTPException(status_code=401, detail="invalid receivables session")
    raw_department_refs = payload.get("department_refs") or []
    if not isinstance(raw_department_refs, list):
        raise HTTPException(status_code=401, detail="invalid receivables session")
    department_refs = expand_receivable_department_refs(
        value for value in (_normalize_ref(item) for item in raw_department_refs) if value
    )
    if access_level == "department" and not department_refs:
        raise HTTPException(status_code=403, detail="не найдено подразделение для доступа")

    return ReceivablesSession(
        actor=f"bitrix:{member_id}:{user_id}",
        domain=domain,
        member_id=member_id,
        user_id=user_id,
        user_name=_normalize_ref(payload.get("user_name")),
        access_level=access_level,
        department_refs=department_refs,
        expires_at=datetime.fromtimestamp(expires_at_ts, UTC),
    )
