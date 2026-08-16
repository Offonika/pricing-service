"""Canonical executable scope policy for display calculation contours.

The business source of truth is ``docs/specs/assortment-lifecycle-policy.md``.
This module deliberately contains only deterministic normalization, exclusion
and audit helpers so every facts/family/order/backtest entrypoint can share the
same gate.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

DISPLAY_SCOPE_POLICY_VERSION = "display_scope_policy.v1"
EXCLUDED_DISPLAY_NAME_BITOK = "excluded_display_name_bitok"

_BITOK_WORD_RE = re.compile(r"(?<!\w)биток(?!\w)", re.UNICODE)


def normalize_display_scope_name(value: object | None) -> str:
    """Normalize only what the accepted scope policy explicitly requires."""

    return " ".join(str(value or "").casefold().replace("ё", "е").split())


def display_scope_exclusion_reason(value: object | None) -> str | None:
    """Return the canonical reason when a display name is outside scope."""

    normalized = normalize_display_scope_name(value)
    if _BITOK_WORD_RE.search(normalized):
        return EXCLUDED_DISPLAY_NAME_BITOK
    return None


def display_scope_record_name(record: object) -> str:
    if isinstance(record, Mapping):
        for key in ("name", "nomenclature_name", "name_1c", "short_name_1c"):
            value = record.get(key)
            if value is not None and str(value).strip():
                return " ".join(str(value).strip().split())
        return ""
    return " ".join(str(getattr(record, "name", "") or "").strip().split())


def display_scope_record_code(record: object) -> str:
    if isinstance(record, Mapping):
        for key in (
            "nomenclature_code",
            "code",
            "_Code",
            "code_1c",
            "article",
            "fact_sku",
            "id",
        ):
            value = record.get(key)
            if value is not None and str(value).strip():
                return " ".join(str(value).strip().split())
        return ""
    for attribute in ("code_1c", "article", "fact_sku", "id"):
        value = getattr(record, attribute, None)
        if value is not None and str(value).strip():
            return " ".join(str(value).strip().split())
    return ""


def is_display_scope_included(record: object) -> bool:
    return display_scope_exclusion_reason(display_scope_record_name(record)) is None


@dataclass(frozen=True)
class DisplayScopeFilterResult:
    included: tuple[Any, ...]
    exclusions: tuple[dict[str, str], ...]
    source_item_count: int
    excluded_row_count: int

    @property
    def audit(self) -> dict[str, Any]:
        reason_counts = Counter(row["reason_code"] for row in self.exclusions)
        return {
            "scope_policy_version": DISPLAY_SCOPE_POLICY_VERSION,
            "source_item_count": self.source_item_count,
            "included_item_count": len(self.included),
            "excluded_item_count": len(self.exclusions),
            "excluded_row_count": self.excluded_row_count,
            "excluded_reason_counts": dict(sorted(reason_counts.items())),
            "exclusions": [dict(row) for row in self.exclusions],
        }


def filter_display_scope_records(records: Sequence[Any]) -> DisplayScopeFilterResult:
    """Apply the common pre-calculation gate and build a unique audit registry."""

    included: list[Any] = []
    exclusions_by_identity: dict[tuple[str, str], dict[str, str]] = {}
    excluded_row_count = 0
    for record in records:
        name = display_scope_record_name(record)
        reason = display_scope_exclusion_reason(name)
        if reason is None:
            included.append(record)
            continue
        excluded_row_count += 1
        code = display_scope_record_code(record)
        identity = (code.casefold(), normalize_display_scope_name(name))
        exclusions_by_identity.setdefault(
            identity,
            {
                "nomenclature_code": code,
                "name": name,
                "reason_code": reason,
                "scope_policy_version": DISPLAY_SCOPE_POLICY_VERSION,
            },
        )

    exclusions = tuple(
        exclusions_by_identity[key]
        for key in sorted(exclusions_by_identity, key=lambda value: (value[0], value[1]))
    )
    return DisplayScopeFilterResult(
        included=tuple(included),
        exclusions=exclusions,
        source_item_count=len(records),
        excluded_row_count=excluded_row_count,
    )


def empty_display_scope_audit(*, source_item_count: int = 0) -> dict[str, Any]:
    return {
        "scope_policy_version": DISPLAY_SCOPE_POLICY_VERSION,
        "source_item_count": source_item_count,
        "included_item_count": source_item_count,
        "excluded_item_count": 0,
        "excluded_row_count": 0,
        "excluded_reason_counts": {},
        "exclusions": [],
    }


def merge_display_scope_audits(*audits: Mapping[str, Any]) -> dict[str, Any]:
    """Merge sequential gates without duplicating technical exclusion rows."""

    usable = [audit for audit in audits if audit]
    if not usable:
        return empty_display_scope_audit()
    exclusions_by_identity: dict[tuple[str, str], dict[str, str]] = {}
    for audit in usable:
        version = str(audit.get("scope_policy_version") or "")
        if version and version != DISPLAY_SCOPE_POLICY_VERSION:
            raise ValueError(f"unsupported_display_scope_policy_version:{version}")
        for raw in audit.get("exclusions") or ():
            row = dict(raw)
            code = str(row.get("nomenclature_code") or "").strip()
            name = str(row.get("name") or "").strip()
            identity = (code.casefold(), normalize_display_scope_name(name))
            row.setdefault("reason_code", EXCLUDED_DISPLAY_NAME_BITOK)
            row.setdefault("scope_policy_version", DISPLAY_SCOPE_POLICY_VERSION)
            exclusions_by_identity.setdefault(identity, row)
    exclusions = [
        exclusions_by_identity[key]
        for key in sorted(exclusions_by_identity, key=lambda value: (value[0], value[1]))
    ]
    reason_counts = Counter(str(row.get("reason_code") or "") for row in exclusions)
    source_item_count = int(usable[0].get("source_item_count") or 0)
    included_item_count = int(usable[-1].get("included_item_count") or 0)
    return {
        "scope_policy_version": DISPLAY_SCOPE_POLICY_VERSION,
        "source_item_count": source_item_count,
        "included_item_count": included_item_count,
        "excluded_item_count": len(exclusions),
        "excluded_row_count": sum(int(audit.get("excluded_row_count") or 0) for audit in usable),
        "excluded_reason_counts": dict(sorted(reason_counts.items())),
        "exclusions": exclusions,
    }
