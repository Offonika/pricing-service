from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Sequence
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.customer_settlement import CustomerSettlementReconciliationRun
from app.services.customer_settlement_source import (
    CustomerSettlementSourceResult,
    ManualCustomerSettlementControl,
)
from app.services.customer_settlements import normalize_money
from app.services.importers.onec_mutual_settlements import (
    OneCMutualSettlementCurrentBalanceRow,
)

RECONCILIATION_TOLERANCE = Decimal("0.01")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class CustomerSettlementReconciliationError(RuntimeError):
    pass


@dataclass(frozen=True)
class CustomerSettlementReconciliationResult:
    report_date: date
    as_of: datetime
    report_hash: str
    context_hash: str
    source_hash: str
    input_hash: str
    status: str
    expected_count: int
    matched_count: int
    mismatch_count: int
    max_abs_difference: Decimal


def end_of_day_boundary_utc(report_date: date, timezone_name: str = "Europe/Moscow") -> datetime:
    local_boundary = datetime.combine(
        report_date + timedelta(days=1),
        time.min,
        tzinfo=ZoneInfo(timezone_name),
    )
    return local_boundary.astimezone(UTC)


def _canonical_name(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split()).casefold()


def _payload_hash(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def customer_settlement_reconciliation_context_hash(
    *,
    mapping_source_hash: str,
    organization_ref: str,
    organization_guid: str,
    source_mode: str,
    opening_organization_field: str,
    movement_organization_field: str,
    counterparty_refs: Sequence[str],
) -> str:
    normalized_mapping_hash = str(mapping_source_hash or "").strip().lower()
    if not _SHA256_RE.fullmatch(normalized_mapping_hash):
        raise CustomerSettlementReconciliationError("mapping_source_hash_is_invalid")
    normalized_refs = tuple(sorted({str(value).strip().lower() for value in counterparty_refs}))
    if not normalized_refs:
        raise CustomerSettlementReconciliationError("pilot_scope_is_empty")
    return _payload_hash(
        {
            "mapping_source_hash": normalized_mapping_hash,
            "organization_ref": str(organization_ref or "").strip().lower(),
            "organization_guid": str(organization_guid or "").strip().lower(),
            "source_mode": str(source_mode or "").strip(),
            "opening_organization_field": str(opening_organization_field or "").strip(),
            "movement_organization_field": str(movement_organization_field or "").strip(),
            "counterparty_refs": normalized_refs,
        }
    )


def customer_settlement_reconciliation_source_hash(
    source: CustomerSettlementSourceResult,
) -> str:
    rows = sorted(source.balances, key=lambda item: str(item.counterparty_ref).lower())
    return _payload_hash(
        {
            "as_of": source.as_of.isoformat(),
            "balances": [
                {
                    "counterparty_ref": str(item.counterparty_ref).strip().lower(),
                    "counterparty_guid": str(item.counterparty_guid or "").strip().lower(),
                    "signed_balance": format(normalize_money(item.signed_balance), ".2f"),
                    "currency": item.currency,
                    "exists": bool(item.exists),
                    "marked_deleted": bool(item.marked_deleted),
                }
                for item in rows
            ],
        }
    )


def reconcile_customer_settlement_rows(
    *,
    report_hash: str,
    context_hash: str,
    report_rows: list[OneCMutualSettlementCurrentBalanceRow],
    controls: tuple[ManualCustomerSettlementControl, ...],
    source: CustomerSettlementSourceResult,
    tolerance: Decimal = RECONCILIATION_TOLERANCE,
) -> CustomerSettlementReconciliationResult:
    normalized_report_hash = str(report_hash or "").strip().lower()
    normalized_context_hash = str(context_hash or "").strip().lower()
    if not _SHA256_RE.fullmatch(normalized_report_hash):
        raise CustomerSettlementReconciliationError("report_hash_is_invalid")
    if not _SHA256_RE.fullmatch(normalized_context_hash):
        raise CustomerSettlementReconciliationError("reconciliation_context_hash_is_invalid")
    report_dates = {item.snapshot_date for item in report_rows}
    if len(report_dates) != 1:
        raise CustomerSettlementReconciliationError("report_date_is_missing_or_ambiguous")
    report_date = next(iter(report_dates))
    expected_as_of = end_of_day_boundary_utc(report_date)
    if source.as_of != expected_as_of:
        raise CustomerSettlementReconciliationError("source_as_of_does_not_match_report_day")

    control_names = [_canonical_name(item.counterparty_name) for item in controls]
    if len(set(control_names)) != len(control_names):
        raise CustomerSettlementReconciliationError("duplicate_pilot_name_in_controls")

    report_by_name: dict[str, Decimal] = {}
    duplicate_names: set[str] = set()
    for item in report_rows:
        name = _canonical_name(item.counterparty_name)
        if name in report_by_name:
            duplicate_names.add(name)
        report_by_name[name] = normalize_money(item.current_balance_rub)

    source_by_ref = {item.counterparty_ref: item.signed_balance for item in source.balances}
    if len(source_by_ref) != len(source.balances):
        raise CustomerSettlementReconciliationError("duplicate_counterparty_in_source")
    if len(source_by_ref) != len(controls):
        raise CustomerSettlementReconciliationError("source_pilot_count_mismatch")

    differences: list[Decimal] = []
    for control in controls:
        name = _canonical_name(control.counterparty_name)
        if name in duplicate_names:
            raise CustomerSettlementReconciliationError("duplicate_pilot_name_in_report")
        if name not in report_by_name or control.counterparty_ref not in source_by_ref:
            raise CustomerSettlementReconciliationError("pilot_missing_from_report_or_source")
        differences.append(
            abs(normalize_money(source_by_ref[control.counterparty_ref]) - report_by_name[name])
        )

    matched_count = sum(value <= tolerance for value in differences)
    mismatch_count = len(differences) - matched_count
    max_difference = max(differences, default=Decimal("0.00"))
    source_hash = customer_settlement_reconciliation_source_hash(source)
    input_hash = _payload_hash(
        {
            "report_hash": normalized_report_hash,
            "context_hash": normalized_context_hash,
            "source_hash": source_hash,
        }
    )
    return CustomerSettlementReconciliationResult(
        report_date=report_date,
        as_of=expected_as_of,
        report_hash=normalized_report_hash,
        context_hash=normalized_context_hash,
        source_hash=source_hash,
        input_hash=input_hash,
        status="matched" if mismatch_count == 0 else "mismatched",
        expected_count=len(controls),
        matched_count=matched_count,
        mismatch_count=mismatch_count,
        max_abs_difference=normalize_money(max_difference),
    )


def report_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def store_reconciliation_result(
    session: Session,
    result: CustomerSettlementReconciliationResult,
) -> CustomerSettlementReconciliationRun:
    existing = session.scalar(
        select(CustomerSettlementReconciliationRun).where(
            CustomerSettlementReconciliationRun.input_hash == result.input_hash,
        )
    )
    if existing is not None:
        return existing
    row = CustomerSettlementReconciliationRun(
        report_date=result.report_date,
        as_of=result.as_of,
        report_hash=result.report_hash,
        context_hash=result.context_hash,
        source_hash=result.source_hash,
        input_hash=result.input_hash,
        status=result.status,
        expected_count=result.expected_count,
        matched_count=result.matched_count,
        mismatch_count=result.mismatch_count,
        max_abs_difference=result.max_abs_difference,
    )
    session.add(row)
    session.flush()
    return row
