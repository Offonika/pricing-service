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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.customer_settlement import (
    CustomerSettlementMappingRevision,
    CustomerSettlementReconciliationRun,
)
from app.services.customer_settlement_source import (
    CustomerSettlementSourceResult,
    ManualCustomerSettlementControl,
)
from app.services.customer_settlements import (
    active_pilot_counterparty_refs,
    ensure_utc,
    normalize_counterparty_ref,
    normalize_guid,
    normalize_money,
    onec_guid_to_ref,
    onec_ref_to_guid,
    try_customer_settlement_context_read_lock,
    utc_now,
)
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
    raw_refs = tuple(counterparty_refs)
    try:
        normalized_organization_ref = normalize_counterparty_ref(organization_ref)
        normalized_organization_guid = normalize_guid(organization_guid)
        normalized_refs = tuple(sorted(normalize_counterparty_ref(value) for value in raw_refs))
    except (TypeError, ValueError) as exc:
        raise CustomerSettlementReconciliationError(
            "reconciliation_context_identity_is_invalid"
        ) from exc
    if onec_guid_to_ref(normalized_organization_guid) != normalized_organization_ref:
        raise CustomerSettlementReconciliationError("reconciliation_context_identity_is_invalid")
    if not 0 < len(normalized_refs) <= 10:
        raise CustomerSettlementReconciliationError("pilot_scope_is_invalid")
    if len(set(normalized_refs)) != len(normalized_refs):
        raise CustomerSettlementReconciliationError("pilot_scope_has_duplicates")
    normalized_source_mode = str(source_mode or "").strip()
    normalized_opening_field = str(opening_organization_field or "").strip()
    normalized_movement_field = str(movement_organization_field or "").strip()
    return _payload_hash(
        {
            "mapping_source_hash": normalized_mapping_hash,
            "organization_ref": normalized_organization_ref,
            "organization_guid": normalized_organization_guid,
            "source_mode": normalized_source_mode,
            "opening_organization_field": normalized_opening_field,
            "movement_organization_field": normalized_movement_field,
            "counterparty_refs": normalized_refs,
        }
    )


def customer_settlement_reconciliation_source_hash(
    source: CustomerSettlementSourceResult,
) -> str:
    rows = sorted(source.balances, key=lambda item: str(item.counterparty_ref).lower())
    return _payload_hash(
        {
            "as_of": ensure_utc(source.as_of).isoformat(),
            "balances": [
                {
                    "counterparty_ref": str(item.counterparty_ref).strip().lower(),
                    "counterparty_guid": normalize_guid(
                        item.counterparty_guid
                        or onec_ref_to_guid(normalize_counterparty_ref(item.counterparty_ref))
                    ),
                    "signed_balance": format(normalize_money(item.signed_balance), ".2f"),
                    "currency": item.currency,
                    "exists": bool(item.exists),
                    "marked_deleted": bool(item.marked_deleted),
                }
                for item in rows
            ],
        }
    )


def customer_settlement_reconciliation_input_hash(
    *,
    report_hash: str,
    context_hash: str,
    source_hash: str,
) -> str:
    hashes = {
        "report_hash": str(report_hash or "").strip().lower(),
        "context_hash": str(context_hash or "").strip().lower(),
        "source_hash": str(source_hash or "").strip().lower(),
    }
    if not all(_SHA256_RE.fullmatch(value) for value in hashes.values()):
        raise CustomerSettlementReconciliationError("reconciliation_input_hash_is_invalid")
    return _payload_hash(hashes)


def latest_customer_settlement_reconciliation(
    session: Session,
) -> CustomerSettlementReconciliationRun | None:
    return session.scalar(
        select(CustomerSettlementReconciliationRun)
        .order_by(CustomerSettlementReconciliationRun.id.desc())
        .limit(1)
    )


def customer_settlement_reconciliation_run_is_current(
    reconciliation: CustomerSettlementReconciliationRun | None,
    *,
    context_hash: str,
    expected_count: int,
) -> bool:
    if reconciliation is None:
        return False
    try:
        max_abs_difference = Decimal(reconciliation.max_abs_difference)
        report_date = reconciliation.report_date
        if not isinstance(report_date, date) or isinstance(report_date, datetime):
            return False
        reconciliation_as_of = ensure_utc(reconciliation.as_of)
        expected_as_of = end_of_day_boundary_utc(report_date)
    except (ArithmeticError, AttributeError, TypeError, ValueError):
        return False
    report_hash = str(reconciliation.report_hash or "")
    stored_context_hash = str(reconciliation.context_hash or "")
    source_hash = str(reconciliation.source_hash or "")
    input_hash = str(reconciliation.input_hash or "")
    try:
        expected_input_hash = customer_settlement_reconciliation_input_hash(
            report_hash=report_hash,
            context_hash=stored_context_hash,
            source_hash=source_hash,
        )
    except CustomerSettlementReconciliationError:
        return False
    hashes_are_canonical = all(
        value == value.strip().lower()
        for value in (report_hash, stored_context_hash, source_hash, input_hash)
    )
    return bool(
        reconciliation.status == "matched"
        and stored_context_hash == context_hash
        and hashes_are_canonical
        and input_hash == expected_input_hash
        and reconciliation.expected_count == expected_count
        and reconciliation.matched_count == expected_count
        and reconciliation.mismatch_count == 0
        and reconciliation_as_of == expected_as_of
        and reconciliation_as_of <= utc_now()
        and max_abs_difference.is_finite()
        and Decimal("0.00") <= max_abs_difference <= Decimal("0.01")
    )


def active_customer_settlement_reconciliation_is_current(
    session: Session,
    *,
    organization_ref: str,
    organization_guid: str,
    source_mode: str,
    opening_organization_field: str,
    movement_organization_field: str,
) -> bool:
    if not try_customer_settlement_context_read_lock(session):
        return False
    mapping = session.scalar(
        select(CustomerSettlementMappingRevision).where(
            CustomerSettlementMappingRevision.status == "active"
        )
    )
    counterparty_refs = active_pilot_counterparty_refs(session)
    if mapping is None or not 0 < len(counterparty_refs) <= 10:
        return False
    try:
        context_hash = customer_settlement_reconciliation_context_hash(
            mapping_source_hash=mapping.source_hash,
            organization_ref=organization_ref,
            organization_guid=organization_guid,
            source_mode=source_mode,
            opening_organization_field=opening_organization_field,
            movement_organization_field=movement_organization_field,
            counterparty_refs=counterparty_refs,
        )
    except CustomerSettlementReconciliationError:
        return False
    return customer_settlement_reconciliation_run_is_current(
        latest_customer_settlement_reconciliation(session),
        context_hash=context_hash,
        expected_count=len(counterparty_refs),
    )


def reconcile_customer_settlement_rows(
    *,
    report_hash: str,
    context_hash: str,
    report_rows: list[OneCMutualSettlementCurrentBalanceRow],
    controls: tuple[ManualCustomerSettlementControl, ...],
    source: CustomerSettlementSourceResult,
    tolerance: Decimal = RECONCILIATION_TOLERANCE,
    report_allows_implicit_zero_rows: bool = False,
) -> CustomerSettlementReconciliationResult:
    try:
        normalized_tolerance = Decimal(str(tolerance))
    except (ArithmeticError, TypeError, ValueError) as exc:
        raise CustomerSettlementReconciliationError("reconciliation_tolerance_is_invalid") from exc
    if not normalized_tolerance.is_finite() or normalized_tolerance != RECONCILIATION_TOLERANCE:
        raise CustomerSettlementReconciliationError("reconciliation_tolerance_is_invalid")
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
    if not 0 < len(controls) <= 10:
        raise CustomerSettlementReconciliationError("reconciliation_pilot_count_is_invalid")

    control_refs: set[str] = set()
    control_guids: set[str] = set()
    for control in controls:
        try:
            counterparty_ref = normalize_counterparty_ref(control.counterparty_ref)
            counterparty_guid = normalize_guid(control.counterparty_guid)
        except (TypeError, ValueError) as exc:
            raise CustomerSettlementReconciliationError(
                "reconciliation_control_identity_is_invalid"
            ) from exc
        if (
            counterparty_ref != control.counterparty_ref
            or counterparty_guid != control.counterparty_guid
            or onec_guid_to_ref(counterparty_guid) != counterparty_ref
            or counterparty_ref in control_refs
            or counterparty_guid in control_guids
        ):
            raise CustomerSettlementReconciliationError(
                "reconciliation_control_identity_is_invalid"
            )
        control_refs.add(counterparty_ref)
        control_guids.add(counterparty_guid)

    control_names = [_canonical_name(item.counterparty_name) for item in controls]
    if any(not name for name in control_names) or len(set(control_names)) != len(control_names):
        raise CustomerSettlementReconciliationError("duplicate_pilot_name_in_controls")

    report_by_name: dict[str, Decimal] = {}
    duplicate_names: set[str] = set()
    for item in report_rows:
        name = _canonical_name(item.counterparty_name)
        if name in report_by_name:
            duplicate_names.add(name)
        report_by_name[name] = normalize_money(item.current_balance_rub)

    source_by_ref: dict[str, Decimal] = {}
    for item in source.balances:
        try:
            counterparty_ref = normalize_counterparty_ref(item.counterparty_ref)
            counterparty_guid = normalize_guid(
                item.counterparty_guid or onec_ref_to_guid(counterparty_ref)
            )
            signed_balance = normalize_money(item.signed_balance)
        except (ArithmeticError, TypeError, ValueError) as exc:
            raise CustomerSettlementReconciliationError(
                "reconciliation_source_identity_is_invalid"
            ) from exc
        if (
            counterparty_ref != item.counterparty_ref
            or (item.counterparty_guid is not None and counterparty_guid != item.counterparty_guid)
            or onec_guid_to_ref(counterparty_guid) != counterparty_ref
            or item.currency != "RUB"
            or not item.exists
            or item.marked_deleted
        ):
            raise CustomerSettlementReconciliationError("reconciliation_source_identity_is_invalid")
        if counterparty_ref in source_by_ref:
            raise CustomerSettlementReconciliationError("duplicate_counterparty_in_source")
        source_by_ref[counterparty_ref] = signed_balance
    if set(source_by_ref) != control_refs:
        raise CustomerSettlementReconciliationError("source_pilot_count_mismatch")

    differences: list[Decimal] = []
    for control in controls:
        name = _canonical_name(control.counterparty_name)
        if name in duplicate_names:
            raise CustomerSettlementReconciliationError("duplicate_pilot_name_in_report")
        if control.counterparty_ref not in source_by_ref:
            raise CustomerSettlementReconciliationError("pilot_missing_from_report_or_source")
        source_balance = normalize_money(source_by_ref[control.counterparty_ref])
        if name not in report_by_name:
            if report_allows_implicit_zero_rows is True and source_balance == Decimal("0.00"):
                differences.append(Decimal("0.00"))
                continue
            raise CustomerSettlementReconciliationError("pilot_missing_from_report_or_source")
        differences.append(abs(source_balance - report_by_name[name]))

    matched_count = sum(value <= normalized_tolerance for value in differences)
    mismatch_count = len(differences) - matched_count
    max_difference = max(differences, default=Decimal("0.00"))
    source_hash = customer_settlement_reconciliation_source_hash(source)
    input_hash = customer_settlement_reconciliation_input_hash(
        report_hash=normalized_report_hash,
        context_hash=normalized_context_hash,
        source_hash=source_hash,
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


def _stored_reconciliation_matches(
    row: CustomerSettlementReconciliationRun,
    result: CustomerSettlementReconciliationResult,
) -> bool:
    return (
        row.report_date == result.report_date
        and ensure_utc(row.as_of) == ensure_utc(result.as_of)
        and row.report_hash == result.report_hash
        and row.context_hash == result.context_hash
        and row.source_hash == result.source_hash
        and row.input_hash == result.input_hash
        and row.status == result.status
        and row.expected_count == result.expected_count
        and row.matched_count == result.matched_count
        and row.mismatch_count == result.mismatch_count
        and normalize_money(row.max_abs_difference) == normalize_money(result.max_abs_difference)
    )


def _validate_stored_reconciliation(
    session: Session,
    row: CustomerSettlementReconciliationRun,
    result: CustomerSettlementReconciliationResult,
) -> CustomerSettlementReconciliationRun:
    if not _stored_reconciliation_matches(row, result):
        raise CustomerSettlementReconciliationError("reconciliation_result_payload_mismatch")
    latest_id = session.scalar(
        select(CustomerSettlementReconciliationRun.id)
        .order_by(CustomerSettlementReconciliationRun.id.desc())
        .limit(1)
    )
    if latest_id != row.id:
        raise CustomerSettlementReconciliationError("reconciliation_result_is_superseded")
    return row


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
        return _validate_stored_reconciliation(session, existing, result)
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
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        existing = session.scalar(
            select(CustomerSettlementReconciliationRun).where(
                CustomerSettlementReconciliationRun.input_hash == result.input_hash,
            )
        )
        if existing is None:
            raise
        return _validate_stored_reconciliation(session, existing, result)
    return row
