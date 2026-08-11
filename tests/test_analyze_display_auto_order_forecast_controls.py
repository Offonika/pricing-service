from datetime import date
from decimal import Decimal

from tasks.analyze_display_auto_order_forecast_controls import (
    attach_rolling_buffers,
    build_decision_windows,
    match_cases_to_controls,
    nearest_rank,
    select_case_anchors,
)


def test_nearest_rank_uses_requested_percentile() -> None:
    values = [Decimal(value) for value in ("0", "1", "2", "3", "10")]

    assert nearest_rank(values, Decimal("0.60")) == Decimal("2")
    assert nearest_rank(values, Decimal("0.75")) == Decimal("3")
    assert nearest_rank(values, Decimal("0.90")) == Decimal("10")


def test_decision_window_excludes_sales_after_fully_observed_horizon() -> None:
    decision_date = date(2026, 3, 1)
    rows = build_decision_windows(
        decision_rows_by_date={
            decision_date: [
                {
                    "scheduled_review": "1",
                    "status": "sale",
                    "nomenclature_code": "SKU-1",
                    "forecast_rate_sales": "1",
                    "lead_time_p50_days": "2",
                    "lead_time_confidence": "high",
                    "inventory_cost_per_unit_rub": "100",
                }
            ]
        },
        sales_by_code={
            "SKU-1": {
                date(2026, 3, 2): Decimal("3"),
                date(2026, 3, 10): Decimal("4"),
                date(2026, 3, 11): Decimal("100"),
            }
        },
        loss_by_code={"SKU-1": {date(2026, 3, 10): Decimal("2")}},
        pattern_by_code={"SKU-1": "intermittent"},
        date_to=date(2026, 3, 31),
    )

    assert len(rows) == 1
    assert rows[0]["outcome_end"] == "2026-03-10"
    assert rows[0]["actual_demand_qty"] == "7"
    assert rows[0]["forecast_demand_qty"] == "9"
    assert rows[0]["underforecast_error_qty"] == "0"
    assert rows[0]["model_lost_observed_qty_in_horizon"] == "2"


def test_case_anchor_is_latest_scheduled_window_covering_loss_start() -> None:
    opportunities = [
        {
            "opportunity_id": "SKU-1:2026-03-01",
            "nomenclature_code": "SKU-1",
            "decision_date": "2026-03-01",
            "outcome_end": "2026-03-20",
        },
        {
            "opportunity_id": "SKU-1:2026-03-08",
            "nomenclature_code": "SKU-1",
            "decision_date": "2026-03-08",
            "outcome_end": "2026-03-27",
        },
    ]
    episodes = [
        {
            "nomenclature_code": "SKU-1",
            "status": "sale",
            "episode_start": "2026-03-15",
            "episode_end": "2026-03-16",
            "lost_observed_qty": "4",
        }
    ]

    anchors = select_case_anchors(opportunities, episodes)

    assert len(anchors) == 1
    assert anchors[0]["opportunity_id"] == "SKU-1:2026-03-08"
    assert anchors[0]["episode_lost_observed_qty"] == "4"


def _matching_row(code: str, rate: str) -> dict[str, str]:
    return {
        "opportunity_id": f"{code}:2026-03-01",
        "nomenclature_code": code,
        "decision_date": "2026-03-01",
        "demand_pattern_preperiod": "intermittent",
        "lead_time_band": "31-60",
        "lead_time_confidence": "high",
        "period": "pre_july",
        "forecast_rate_band": "(0.10;0.50]",
        "forecast_rate_sales": rate,
        "inventory_cost_per_unit_rub": "100",
        "cost_band": "<500",
        "episode_lost_observed_qty": "2",
    }


def test_matching_does_not_reuse_one_control() -> None:
    cases = [_matching_row("CASE-1", "0.2"), _matching_row("CASE-2", "0.3")]
    controls = [_matching_row("CONTROL-1", "0.25")]

    pairs, unmatched = match_cases_to_controls(cases, controls)

    assert len(pairs) == 1
    assert len(unmatched) == 1
    assert len({row["control_opportunity_id"] for row in pairs}) == 1


def test_rolling_buffer_uses_only_previously_completed_windows() -> None:
    common = {
        "demand_pattern_preperiod": "smooth",
        "lead_time_band": "31-60",
        "lead_time_confidence": "high",
        "forecast_rate_band": "(0.10;0.50]",
    }
    opportunities = [
        {
            **common,
            "opportunity_id": "A:2026-03-01",
            "decision_date": "2026-03-01",
            "outcome_end": "2026-03-03",
            "underforecast_error_qty": "2",
        },
        {
            **common,
            "opportunity_id": "B:2026-03-04",
            "decision_date": "2026-03-04",
            "outcome_end": "2026-03-06",
            "underforecast_error_qty": "100",
        },
    ]

    rows = attach_rolling_buffers(opportunities, min_samples=1)
    by_id = {row["opportunity_id"]: row for row in rows}

    assert by_id["A:2026-03-01"]["calibration_level"] == "insufficient"
    assert by_id["B:2026-03-04"]["buffer_p60_qty"] == "2"
    assert by_id["B:2026-03-04"]["buffer_p75_qty"] == "2"
    assert by_id["B:2026-03-04"]["buffer_p90_qty"] == "2"
