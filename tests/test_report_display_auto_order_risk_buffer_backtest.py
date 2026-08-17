from decimal import Decimal

import pytest

from tasks.report_display_auto_order_risk_buffer_backtest import build_buffer_schedules


def _risk_row(
    opportunity_id: str,
    *,
    expected: str,
    probability: str = "0.5",
) -> dict[str, str]:
    return {
        "opportunity_id": opportunity_id,
        "nomenclature_code": opportunity_id,
        "decision_date": "2026-06-01",
        "risk_training_sufficient": "1",
        "shortage_expected_qty": expected,
        "shortage_risk_probability": probability,
    }


def test_build_buffer_schedules_preserves_v19_and_adds_only_selected_extra() -> None:
    risk_rows = [
        _risk_row("a", expected="10"),
        _risk_row("b", expected="8"),
        _risk_row("c", expected="1"),
        _risk_row("d", expected="0"),
    ]
    allocation_rows = [
        {
            "strategy": "economic_extra_0.50",
            "decision_date": "2026-06-01",
            "nomenclature_code": "c",
            "baseline_buffer_qty": "0",
            "allocated_extra_qty": "3",
        },
        {
            "strategy": "economic_extra_0.25",
            "decision_date": "2026-06-01",
            "nomenclature_code": "d",
            "baseline_buffer_qty": "0",
            "allocated_extra_qty": "99",
        },
    ]

    baseline, candidate, rows = build_buffer_schedules(risk_rows, allocation_rows)

    rendered_baseline = {(day.isoformat(), code): qty for (day, code), qty in baseline.items()}
    rendered_candidate = {(day.isoformat(), code): qty for (day, code), qty in candidate.items()}
    assert rendered_baseline == {
        ("2026-06-01", "a"): Decimal("10"),
        ("2026-06-01", "b"): Decimal("8"),
    }
    assert rendered_candidate == {
        **rendered_baseline,
        ("2026-06-01", "c"): Decimal("3"),
    }
    assert all(candidate[key] >= quantity for key, quantity in baseline.items())
    assert next(row for row in rows if row["nomenclature_code"] == "c")["economic_extra_qty"] == "3"


def test_build_buffer_schedules_rejects_changed_baseline_contract() -> None:
    with pytest.raises(ValueError, match="baseline mismatch"):
        build_buffer_schedules(
            [
                _risk_row("a", expected="10"),
                _risk_row("b", expected="0"),
            ],
            [
                {
                    "strategy": "economic_extra_0.50",
                    "decision_date": "2026-06-01",
                    "nomenclature_code": "a",
                    "baseline_buffer_qty": "9",
                    "allocated_extra_qty": "1",
                }
            ],
        )
