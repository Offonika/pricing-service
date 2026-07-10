from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

MANUAL_STAFF_REF_PREFIX = "manual-staff:"
DEFAULT_RULES_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "receivables" / "staff-contact-overrides.json"
)


@dataclass(frozen=True)
class StaffDepartmentOverride:
    name_marker: str
    department_name: str


@dataclass(frozen=True)
class FallbackStaffMember:
    staff_ref: str
    full_name: str
    department_name: str
    department_ref: str | None = None


@dataclass(frozen=True)
class ReceivableStaffContactRules:
    exclude_name_markers: tuple[str, ...]
    department_overrides: tuple[StaffDepartmentOverride, ...]
    fallback_staff: tuple[FallbackStaffMember, ...]
    next_source_note: str | None = None


def _normalize_name(value: object) -> str:
    return " ".join(str(value or "").strip().casefold().replace("ё", "е").split())


def manual_staff_ref(full_name: str, department_name: str) -> str:
    normalized = f"{_normalize_name(full_name)}|{_normalize_name(department_name)}"
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]
    return f"{MANUAL_STAFF_REF_PREFIX}{digest}"


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in (_normalize_name(raw) for raw in value) if item)


def _department_overrides(value: Any) -> tuple[StaffDepartmentOverride, ...]:
    if not isinstance(value, list):
        return ()
    overrides: list[StaffDepartmentOverride] = []
    for raw in value:
        if not isinstance(raw, dict):
            continue
        marker = _normalize_name(raw.get("name_marker"))
        department_name = str(raw.get("department_name") or "").strip()
        if marker and department_name:
            overrides.append(
                StaffDepartmentOverride(
                    name_marker=marker,
                    department_name=department_name,
                )
            )
    return tuple(overrides)


def _fallback_staff(value: Any) -> tuple[FallbackStaffMember, ...]:
    if not isinstance(value, list):
        return ()
    result: list[FallbackStaffMember] = []
    for raw in value:
        if not isinstance(raw, dict):
            continue
        full_name = str(raw.get("full_name") or "").strip()
        department_name = str(raw.get("department_name") or "").strip()
        if not full_name or not department_name:
            continue
        result.append(
            FallbackStaffMember(
                staff_ref=str(raw.get("staff_ref") or "").strip()
                or manual_staff_ref(full_name, department_name),
                full_name=full_name,
                department_name=department_name,
                department_ref=str(raw.get("department_ref") or "").strip() or None,
            )
        )
    return tuple(result)


@lru_cache(maxsize=8)
def load_receivable_staff_contact_rules(
    rules_path: str | os.PathLike[str] | None = None,
) -> ReceivableStaffContactRules:
    path = Path(
        rules_path or os.getenv("RECEIVABLE_STAFF_CONTACT_RULES_PATH") or DEFAULT_RULES_PATH
    )
    if not path.exists():
        return ReceivableStaffContactRules(
            exclude_name_markers=(),
            department_overrides=(),
            fallback_staff=(),
        )
    with path.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    if not isinstance(payload, dict):
        return ReceivableStaffContactRules(
            exclude_name_markers=(),
            department_overrides=(),
            fallback_staff=(),
        )
    return ReceivableStaffContactRules(
        exclude_name_markers=_string_tuple(payload.get("exclude_name_markers")),
        department_overrides=_department_overrides(payload.get("department_overrides")),
        fallback_staff=_fallback_staff(payload.get("fallback_staff")),
        next_source_note=str(payload.get("next_source_note") or "").strip() or None,
    )
