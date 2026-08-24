from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.services.customer_settlement_receivable_drift import (
    CustomerSettlementReceivableDriftError,
    build_customer_settlement_receivable_reconciliation,
    compare_customer_settlement_with_receivables,
)
from app.services.customer_settlement_source import CustomerSettlementSourceResult
from app.services.customer_settlements import SettlementBalanceInput
from tasks import check_customer_settlement_receivable_drift as task


def _ref(index: int) -> str:
    return f"0x{index:032x}"


def _compare(*, source_rows, receivable_rows, expected_pilot_count=3):
    return compare_customer_settlement_with_receivables(
        completed_date=date(2026, 8, 24),
        source_as_of=datetime(2026, 8, 24, 21, tzinfo=timezone.utc),
        expected_pilot_count=expected_pilot_count,
        source_rows=source_rows,
        receivable_rows=receivable_rows,
    )


def test_missing_receivable_row_matches_only_explicit_source_zero() -> None:
    result = _compare(
        source_rows=[
            (_ref(1), Decimal("100.00")),
            (_ref(2), Decimal("-10.00")),
            (_ref(3), Decimal("0.00")),
        ],
        receivable_rows=[
            (_ref(1).upper().replace("0X", "0x"), Decimal("100.00")),
            (_ref(2), Decimal("-10.00")),
        ],
    )

    assert result.status == "ok"
    assert result.matched_count == 3
    assert result.missing_zero_count == 1
    assert result.missing_nonzero_count == 0
    assert result.source_states == {"debt": 1, "advance": 1, "zero": 1}
    assert "counterparty_ref" not in result.safe_payload()


def test_missing_nonzero_receivable_row_is_critical() -> None:
    result = _compare(
        source_rows=[
            (_ref(1), Decimal("100.00")),
            (_ref(2), Decimal("-10.00")),
            (_ref(3), Decimal("1.00")),
        ],
        receivable_rows=[
            (_ref(1), Decimal("100.00")),
            (_ref(2), Decimal("-10.00")),
        ],
    )

    assert result.status == "critical"
    assert result.missing_nonzero_count == 1
    assert result.matched_count == 2


def test_difference_above_tolerance_is_critical() -> None:
    result = _compare(
        source_rows=[
            (_ref(1), Decimal("100.00")),
            (_ref(2), Decimal("-10.00")),
            (_ref(3), Decimal("0.00")),
        ],
        receivable_rows=[
            (_ref(1), Decimal("100.02")),
            (_ref(2), Decimal("-10.00")),
        ],
    )

    assert result.status == "critical"
    assert result.mismatch_count == 1
    assert result.matched_count == 2


def test_difference_at_tolerance_matches() -> None:
    result = _compare(
        source_rows=[
            (_ref(1), Decimal("100.00")),
            (_ref(2), Decimal("-10.00")),
            (_ref(3), Decimal("0.00")),
        ],
        receivable_rows=[
            (_ref(1), Decimal("100.01")),
            (_ref(2), Decimal("-10.00")),
        ],
    )

    assert result.status == "ok"
    assert result.matched_count == 3
    assert result.max_abs_difference == Decimal("0.01")


def test_receivable_checkpoint_builds_current_reconciliation_evidence() -> None:
    completed_date = date(2026, 8, 24)
    source_as_of = datetime(2026, 8, 24, 21, tzinfo=timezone.utc)
    result, drift = build_customer_settlement_receivable_reconciliation(
        context_hash="f" * 64,
        source=CustomerSettlementSourceResult(
            source_db_time=source_as_of + timedelta(minutes=1),
            as_of=source_as_of,
            balances=(
                SettlementBalanceInput(_ref(1), Decimal("100.00")),
                SettlementBalanceInput(_ref(2), Decimal("0.00")),
            ),
            isolation_level="READ COMMITTED",
            duration_seconds=0.1,
        ),
        completed_date=completed_date,
        expected_count=2,
        receivable_rows=[(_ref(1), Decimal("100.00"))],
        receivable_total_rows=10,
    )

    assert drift.status == "ok"
    assert result.status == "matched"
    assert result.expected_count == result.matched_count == 2
    assert result.mismatch_count == 0
    assert result.max_abs_difference == Decimal("0.00")
    assert len(result.report_hash) == len(result.source_hash) == len(result.input_hash) == 64


def test_duplicate_source_counterparty_is_rejected() -> None:
    with pytest.raises(
        CustomerSettlementReceivableDriftError,
        match="duplicate_source_counterparty",
    ):
        _compare(
            source_rows=[
                (_ref(1), Decimal("100.00")),
                (_ref(1), Decimal("100.00")),
                (_ref(3), Decimal("0.00")),
            ],
            receivable_rows=[],
        )


def test_receivable_env_file_requires_secure_absolute_postgres_url(tmp_path) -> None:
    env_file = tmp_path / "receivables.env"
    env_file.write_text(
        "DATABASE_URL=postgresql+psycopg2://readonly:secret@localhost/pricing\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)

    database_url = task._read_database_url_from_env_file(str(env_file))

    assert database_url.startswith("postgresql+psycopg2://")


def test_receivable_env_file_rejects_group_writable_file(tmp_path) -> None:
    env_file = tmp_path / "receivables.env"
    env_file.write_text(
        "DATABASE_URL=postgresql+psycopg2://readonly:secret@localhost/pricing\n",
        encoding="utf-8",
    )
    env_file.chmod(0o620)

    with pytest.raises(
        CustomerSettlementReceivableDriftError,
        match="receivable_env_file_is_not_secure",
    ):
        task._read_database_url_from_env_file(str(env_file))


def test_receivable_env_file_rejects_non_postgres_url(tmp_path) -> None:
    env_file = tmp_path / "receivables.env"
    env_file.write_text("DATABASE_URL=sqlite:///unsafe.db\n", encoding="utf-8")
    env_file.chmod(0o600)

    with pytest.raises(
        CustomerSettlementReceivableDriftError,
        match="receivable_database_is_not_postgresql",
    ):
        task._read_database_url_from_env_file(str(env_file))
