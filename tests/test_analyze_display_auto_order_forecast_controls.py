from datetime import date
from decimal import Decimal

from tasks.analyze_display_auto_order_forecast_controls import (
    attach_rolling_buffers,
    build_decision_windows,
    evaluate_buffer_policy,
    match_cases_to_controls,
    nearest_rank,
    segment_stability_rows,
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
    assert rows[0]["period"] == "final_month_exposed"


def test_decision_window_period_uses_outcome_end_not_decision_month() -> None:
    rows = build_decision_windows(
        decision_rows_by_date={
            date(2026, 5, 1): [
                {
                    "scheduled_review": "1",
                    "status": "sale",
                    "nomenclature_code": "SKU-1",
                    "forecast_rate_sales": "0",
                    "lead_time_p50_days": "60",
                    "lead_time_confidence": "medium",
                    "inventory_cost_per_unit_rub": "100",
                }
            ]
        },
        sales_by_code={"SKU-1": {}},
        loss_by_code={"SKU-1": {}},
        pattern_by_code={"SKU-1": "smooth"},
        date_to=date(2026, 7, 31),
    )

    assert rows[0]["decision_period"] == "pre_final_month"
    assert rows[0]["outcome_end"] == "2026-07-07"
    assert rows[0]["period"] == "final_month_exposed"


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


def _policy_pair(
    pair_id: int,
    *,
    period: str,
    case_error: str,
    control_error: str,
    loss: str = "3",
) -> dict[str, str]:
    return {
        "pair_id": str(pair_id),
        "case_opportunity_id": f"CASE-{pair_id}",
        "control_opportunity_id": f"CONTROL-{pair_id}",
        "case_pattern": "smooth",
        "case_lead_band": "31-60",
        "case_confidence": "medium",
        "case_period": period,
        "control_period": period,
        "case_underforecast_error_qty": case_error,
        "control_underforecast_error_qty": control_error,
        "case_episode_lost_qty": loss,
    }


def test_segment_candidate_is_selected_only_from_pre_final_evidence() -> None:
    pairs = [
        _policy_pair(
            1,
            period="pre_final_month",
            case_error="3",
            control_error="0",
        ),
        _policy_pair(
            2,
            period="pre_final_month",
            case_error="2",
            control_error="0",
        ),
        _policy_pair(
            3,
            period="final_month_exposed",
            case_error="0",
            control_error="5",
        ),
    ]

    rows = segment_stability_rows(
        pairs,
        candidate_min_pairs=2,
        candidate_min_gap=Decimal("1"),
    )

    assert len(rows) == 1
    assert rows[0]["candidate_p90"] == 1
    assert Decimal(rows[0]["pre_final_mean_underforecast_gap_qty"]) > 0
    assert Decimal(rows[0]["final_month_mean_underforecast_gap_qty"]) < 0


def test_targeted_policy_uses_p90_only_for_selected_segment() -> None:
    pair = _policy_pair(
        1,
        period="final_month_exposed",
        case_error="4",
        control_error="0",
    )
    opportunities = {
        "CASE-1": {
            "calibration_level": "all",
            "underforecast_error_qty": "4",
            "buffer_p75_qty": "1",
            "buffer_p90_qty": "4",
            "inventory_cost_per_unit_rub": "100",
        },
        "CONTROL-1": {
            "calibration_level": "all",
            "underforecast_error_qty": "0",
            "buffer_p75_qty": "1",
            "buffer_p90_qty": "4",
            "inventory_cost_per_unit_rub": "100",
        },
    }
    selected = {("smooth", "31-60", "medium")}

    result = evaluate_buffer_policy(
        [pair],
        opportunities,
        policy="targeted_p90_else_p75",
        period="final_month_exposed",
        candidate_segments=selected,
    )

    assert result["candidate_pair_count"] == 1
    assert result["case_loss_proxy_covered_qty"] == "3"
    assert result["control_excess_buffer_proxy_qty"] == "4"
