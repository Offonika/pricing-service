from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

MONEY_QUANTUM = Decimal("0.01")
MATCH_TOLERANCE = Decimal("0.01")


class CustomerSettlementReceivableDriftError(RuntimeError):
    """Raised when the automatic receivables checkpoint is not trustworthy."""


def _money(value: object) -> Decimal:
    try:
        amount = Decimal(str(value)).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CustomerSettlementReceivableDriftError("invalid_balance") from exc
    if not amount.is_finite():
        raise CustomerSettlementReceivableDriftError("invalid_balance")
    if amount == 0:
        return Decimal("0.00")
    return amount


def _normalize_ref(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if len(normalized) != 34 or not normalized.startswith("0x"):
        raise CustomerSettlementReceivableDriftError("invalid_counterparty_ref")
    try:
        bytes.fromhex(normalized[2:])
    except ValueError as exc:
        raise CustomerSettlementReceivableDriftError("invalid_counterparty_ref") from exc
    return normalized


def normalize_balance_mapping(
    rows: Iterable[tuple[object, object]],
    *,
    duplicate_error_code: str,
) -> dict[str, Decimal]:
    balances: dict[str, Decimal] = {}
    for raw_ref, raw_balance in rows:
        counterparty_ref = _normalize_ref(raw_ref)
        if counterparty_ref in balances:
            raise CustomerSettlementReceivableDriftError(duplicate_error_code)
        balances[counterparty_ref] = _money(raw_balance)
    return balances


def _state_counts(balances: Mapping[str, Decimal]) -> dict[str, int]:
    return {
        "debt": sum(value > 0 for value in balances.values()),
        "advance": sum(value < 0 for value in balances.values()),
        "zero": sum(value == 0 for value in balances.values()),
    }


@dataclass(frozen=True)
class CustomerSettlementReceivableDriftResult:
    completed_date: date
    source_as_of: datetime
    expected_pilot_count: int
    source_pilot_count: int
    receivable_present_count: int
    matched_count: int
    mismatch_count: int
    missing_zero_count: int
    missing_nonzero_count: int
    unexpected_receivable_count: int
    source_states: dict[str, int]
    receivable_present_states: dict[str, int]

    @property
    def status(self) -> str:
        return (
            "ok"
            if self.source_pilot_count == self.expected_pilot_count
            and self.matched_count == self.expected_pilot_count
            and self.mismatch_count == 0
            and self.missing_nonzero_count == 0
            and self.unexpected_receivable_count == 0
            else "critical"
        )

    def safe_payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "completed_date": self.completed_date.isoformat(),
            "source_as_of": self.source_as_of.isoformat(),
            "expected_pilot_count": self.expected_pilot_count,
            "source_pilot_count": self.source_pilot_count,
            "receivable_present_count": self.receivable_present_count,
            "matched_count": self.matched_count,
            "mismatch_count": self.mismatch_count,
            "missing_zero_count": self.missing_zero_count,
            "missing_nonzero_count": self.missing_nonzero_count,
            "unexpected_receivable_count": self.unexpected_receivable_count,
            "source_states": self.source_states,
            "receivable_present_states": self.receivable_present_states,
        }


def compare_customer_settlement_with_receivables(
    *,
    completed_date: date,
    source_as_of: datetime,
    expected_pilot_count: int,
    source_rows: Iterable[tuple[object, object]],
    receivable_rows: Iterable[tuple[object, object]],
) -> CustomerSettlementReceivableDriftResult:
    if expected_pilot_count <= 0:
        raise CustomerSettlementReceivableDriftError("invalid_expected_pilot_count")
    source_balances = normalize_balance_mapping(
        source_rows,
        duplicate_error_code="duplicate_source_counterparty",
    )
    receivable_balances = normalize_balance_mapping(
        receivable_rows,
        duplicate_error_code="duplicate_receivable_counterparty",
    )

    source_refs = set(source_balances)
    receivable_refs = set(receivable_balances)
    unexpected_refs = receivable_refs - source_refs
    matched_count = 0
    mismatch_count = 0
    missing_zero_count = 0
    missing_nonzero_count = 0
    for counterparty_ref, source_balance in source_balances.items():
        if counterparty_ref not in receivable_balances:
            if source_balance == 0:
                missing_zero_count += 1
                matched_count += 1
            else:
                missing_nonzero_count += 1
            continue
        difference = abs(receivable_balances[counterparty_ref] - source_balance)
        if difference <= MATCH_TOLERANCE:
            matched_count += 1
        else:
            mismatch_count += 1

    return CustomerSettlementReceivableDriftResult(
        completed_date=completed_date,
        source_as_of=source_as_of,
        expected_pilot_count=expected_pilot_count,
        source_pilot_count=len(source_balances),
        receivable_present_count=len(receivable_balances),
        matched_count=matched_count,
        mismatch_count=mismatch_count,
        missing_zero_count=missing_zero_count,
        missing_nonzero_count=missing_nonzero_count,
        unexpected_receivable_count=len(unexpected_refs),
        source_states=_state_counts(source_balances),
        receivable_present_states=_state_counts(receivable_balances),
    )
