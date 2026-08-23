from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
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


class CustomerSettlementReconciliationError(RuntimeError):
    pass


@dataclass(frozen=True)
class CustomerSettlementReconciliationResult:
    report_date: date
    as_of: datetime
    report_hash: str
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


def reconcile_customer_settlement_rows(
    *,
    report_hash: str,
    report_rows: list[OneCMutualSettlementCurrentBalanceRow],
    controls: tuple[ManualCustomerSettlementControl, ...],
    source: CustomerSettlementSourceResult,
    tolerance: Decimal = RECONCILIATION_TOLERANCE,
) -> CustomerSettlementReconciliationResult:
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
    return CustomerSettlementReconciliationResult(
        report_date=report_date,
        as_of=expected_as_of,
        report_hash=report_hash,
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
            CustomerSettlementReconciliationRun.report_date == result.report_date,
            CustomerSettlementReconciliationRun.report_hash == result.report_hash,
        )
    )
    if existing is not None:
        return existing
    row = CustomerSettlementReconciliationRun(
        report_date=result.report_date,
        as_of=result.as_of,
        report_hash=result.report_hash,
        status=result.status,
        expected_count=result.expected_count,
        matched_count=result.matched_count,
        mismatch_count=result.mismatch_count,
        max_abs_difference=result.max_abs_difference,
    )
    session.add(row)
    session.flush()
    return row
