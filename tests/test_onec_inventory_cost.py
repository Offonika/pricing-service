from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from app.services.onec_inventory_cost import (
    CURRENT_INVENTORY_COST_SQL,
    CURRENT_TOTALS_PERIOD,
    HISTORICAL_INVENTORY_COST_SQL,
    UNBILLED_PARTY_STATUS_HEX,
    fetch_onec_inventory_cost,
)


class _FakeMappings:
    def __init__(self, row: dict[str, Any]) -> None:
        self.row = row

    def one(self) -> dict[str, Any]:
        return self.row


class _FakeResult:
    def __init__(self, row: dict[str, Any]) -> None:
        self.row = row

    def mappings(self) -> _FakeMappings:
        return _FakeMappings(self.row)


class _FakeConnection:
    def __init__(self, engine: _FakeEngine) -> None:
        self.engine = engine

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, statement: object, params: dict[str, Any]) -> _FakeResult:
        self.engine.statement = statement
        self.engine.params = params
        return _FakeResult(self.engine.row)


class _FakeEngine:
    def __init__(self, row: dict[str, Any]) -> None:
        self.row = row
        self.statement: object | None = None
        self.params: dict[str, Any] = {}

    def connect(self) -> _FakeConnection:
        return _FakeConnection(self)


def test_current_inventory_cost_reads_verified_onec_totals() -> None:
    engine = _FakeEngine(
        {
            "source_row_count": 757589,
            "quantity": Decimal("993254.000"),
            "amount": Decimal("257265596.37"),
            "negative_cost_row_count": 95,
            "negative_cost_amount": Decimal("-103397.60"),
        }
    )

    snapshot = fetch_onec_inventory_cost(engine, as_of=date.today())  # type: ignore[arg-type]

    assert snapshot.amount == Decimal("257265596.37")
    assert snapshot.quantity == Decimal("993254.000")
    assert snapshot.source_status == "ready"
    assert snapshot.negative_cost_row_count == 95
    assert engine.params == {"current_totals_period": CURRENT_TOTALS_PERIOD}


def test_historical_inventory_cost_uses_opening_and_movements_to_end_of_day() -> None:
    engine = _FakeEngine(
        {
            "source_row_count": 30000,
            "quantity": Decimal("900000.000"),
            "amount": Decimal("240000000.00"),
            "negative_cost_row_count": 0,
            "negative_cost_amount": Decimal("0.00"),
        }
    )

    snapshot = fetch_onec_inventory_cost(engine, as_of=date(2026, 6, 30))  # type: ignore[arg-type]

    assert snapshot.amount == Decimal("240000000.00")
    assert engine.params == {
        "month_start": datetime(2026, 6, 1),
        "date_to": datetime(2026, 7, 1),
    }


def test_mixed_inventory_cost_keeps_party_totals_as_reconciliation_controls() -> None:
    engine = _FakeEngine(
        {
            "source_row_count": 45000,
            "stock_source_row_count": 15000,
            "party_source_row_count": 30000,
            "stock_row_count": 1200,
            "party_row_count": 800,
            "quantity": Decimal("951101.000"),
            "amount": Decimal("250557543.08"),
            "party_quantity": Decimal("951367.000"),
            "party_amount": Decimal("251495551.03"),
            "valuation_party_quantity": Decimal("951366.000"),
            "valuation_party_amount": Decimal("251495551.03"),
            "excluded_party_quantity": Decimal("1.000"),
            "excluded_party_amount": Decimal("0.00"),
            "quantity_difference": Decimal("-266.000"),
            "unmatched_stock_row_count": 3,
            "unmatched_stock_quantity": Decimal("78.000"),
            "unmatched_stock_quantity_abs": Decimal("114.000"),
            "zero_party_quantity_row_count": 1,
            "negative_cost_row_count": 2,
            "negative_cost_amount": Decimal("-42.15"),
        }
    )

    snapshot = fetch_onec_inventory_cost(  # type: ignore[arg-type]
        engine,
        as_of=date(2026, 3, 31),
    )

    assert snapshot.amount == Decimal("250557543.08")
    assert snapshot.quantity == Decimal("951101.000")
    assert snapshot.party_amount == Decimal("251495551.03")
    assert snapshot.party_quantity == Decimal("951367.000")
    assert snapshot.valuation_party_quantity == Decimal("951366.000")
    assert snapshot.excluded_party_quantity == Decimal("1.000")
    assert snapshot.quantity_difference == Decimal("-266.000")
    assert snapshot.unmatched_stock_quantity_abs == Decimal("114.000")
    assert snapshot.source_status == "partial"
    assert snapshot.reconciliation_status == "quantity_mismatch"


def test_zero_party_quantity_remains_visible_instead_of_dividing_by_zero() -> None:
    engine = _FakeEngine(
        {
            "source_row_count": 2,
            "quantity": Decimal("5.000"),
            "amount": Decimal("0.00"),
            "party_quantity": Decimal("0.000"),
            "party_amount": Decimal("0.00"),
            "valuation_party_quantity": Decimal("0.000"),
            "valuation_party_amount": Decimal("0.00"),
            "quantity_difference": Decimal("5.000"),
            "unmatched_stock_row_count": 1,
            "unmatched_stock_quantity": Decimal("5.000"),
            "unmatched_stock_quantity_abs": Decimal("5.000"),
            "zero_party_quantity_row_count": 1,
        }
    )

    snapshot = fetch_onec_inventory_cost(  # type: ignore[arg-type]
        engine,
        as_of=date(2026, 3, 31),
    )

    assert snapshot.amount == Decimal("0.00")
    assert snapshot.party_quantity == Decimal("0.000")
    assert snapshot.unmatched_stock_row_count == 1
    assert snapshot.zero_party_quantity_row_count == 1
    assert snapshot.reconciliation_status == "quantity_mismatch"


def test_sql_matches_ut103_mixed_report_formula_and_rounding() -> None:
    current_sql = str(CURRENT_INVENTORY_COST_SQL)
    historical_sql = str(HISTORICAL_INVENTORY_COST_SQL)

    for sql in (current_sql, historical_sql):
        assert "_AccumRgT7745" in sql
        assert "_AccumRgT7473" in sql
        assert "LEFT JOIN valuation_party" in sql
        assert "party.quantity IS NULL OR party.quantity = 0" in sql
        assert "AS decimal(15, 2)" in sql
        assert f"0x{UNBILLED_PARTY_STATUS_HEX}" in sql
        assert "WHERE is_unbilled = 0" in sql

    assert "_AccumRg7735" in historical_sql
    assert "_AccumRg7453" in historical_sql
