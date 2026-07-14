from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from app.services.onec_inventory_cost import (
    CURRENT_TOTALS_PERIOD,
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
