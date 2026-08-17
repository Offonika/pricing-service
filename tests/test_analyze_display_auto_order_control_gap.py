from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from tasks.analyze_display_auto_order_control_gap import (
    classify_loss_reason,
    group_loss_episodes,
    loss_reason_flags,
)
from tasks.build_display_auto_order_dry_run import AutoOrderPolicy
from tasks.display_auto_order_backtest_preflight import (
    CarryingCostScenario,
    load_scenario_config,
)
from tasks.report_display_auto_order_frozen_backtest import FrozenScenario, simulate_scenario


def test_pipeline_blocking_is_classified_from_prior_causal_state() -> None:
    row = {
        "status": "sale",
        "prior_evaluation_date": "2026-03-01",
        "prior_min_stock_qty": "3",
        "prior_inventory_position_qty": "7",
        "prior_model_stock_qty": "1",
        "prior_reserve_qty": "0",
        "prior_effective_model_pipeline_qty": "6",
        "model_pipeline_qty": "6",
        "prior_recommended_order_qty_raw": "0",
        "prior_recommended_order_qty": "0",
        "prior_forecast_rate_sales": "0.2",
        "current_lead_time_p75_days": "60",
        "prior_selected_lead_time_days": "40",
        "current_lead_time_confidence": "medium",
        "prior_safety_stock_qty": "0",
    }

    flags = loss_reason_flags(row)

    assert flags["pipeline_blocked_reorder"] == 1
    assert flags["pipeline_present"] == 1
    assert flags["lead_time_risk"] == 1
    assert classify_loss_reason(row) == "pipeline_counted_before_arrival"


def test_simulation_records_prior_state_for_observed_loss() -> None:
    start = date(2026, 3, 1)
    loss_date = start + timedelta(days=1)
    facts = {
        start: [
            {
                "nomenclature_code": "SKU-1",
                "status": "sale",
                "physical_stock_qty": "0",
                "observed_sales_qty": "0",
                "effective_reserve_qty": "0",
                "placed_incoming_qty": "0",
            }
        ],
        loss_date: [
            {
                "nomenclature_code": "SKU-1",
                "status": "sale",
                "physical_stock_qty": "2",
                "observed_sales_qty": "2",
                "effective_reserve_qty": "0",
                "placed_incoming_qty": "0",
            }
        ],
    }
    decisions = {
        start: [
            {
                "decision_date": start.isoformat(),
                "nomenclature_code": "SKU-1",
                "scheduled_review": "1",
                "status": "sale",
                "forecast_rate_sales": "0",
                "lead_time_p50_days": "5",
                "lead_time_p75_days": "8",
                "lead_time_confidence": "medium",
                "inventory_cost_per_unit_rub": "100",
                "gross_margin_per_unit_rub": "50",
            }
        ]
    }
    result = simulate_scenario(
        scenario=FrozenScenario(
            scenario_id="control",
            stage_profile="typical",
            kmp4_weight=Decimal("0"),
            cost=CarryingCostScenario(
                name="base",
                capital_annual_rate=Decimal("0.3"),
                storage_annual_rate=Decimal("0.1"),
                obsolescence_annual_rate=Decimal("0.25"),
            ),
        ),
        fact_rows_by_date=facts,
        decision_rows_by_date=decisions,
        initial_pipeline_rows=[],
        sales_by_code={"SKU-1": {loss_date: Decimal("2")}},
        policy=AutoOrderPolicy(order_cadence_days=7),
        config=load_scenario_config(
            Path("config/assortment/display-auto-order-backtest-scenarios.json")
        ),
        date_from=start,
        date_to=loss_date,
        keep_detail=True,
        demand_sample_cache={},
    )

    assert result.model["SKU-1"].lost_observed_qty == Decimal("2")
    assert len(result.loss_rows) == 1
    assert result.loss_rows[0]["business_date"] == loss_date.isoformat()
    assert result.loss_rows[0]["prior_evaluation_date"] == start.isoformat()
    assert result.loss_rows[0]["prior_forecast_rate_sales"] == "0"
    assert len(result.decision_rows) == 1
    assert result.decision_rows[0]["decision_trigger"] == "scheduled_review"


def _loss_row(
    business_date: date,
    *,
    code: str = "SKU-1",
    lost_qty: str = "1",
    prior_evaluation_date: str = "2026-03-01",
) -> dict[str, str]:
    return {
        "business_date": business_date.isoformat(),
        "nomenclature_code": code,
        "name": f"Name {code}",
        "status": "sale",
        "demand_pattern_preperiod": "intermittent",
        "lost_observed_qty": lost_qty,
        "lost_gross_margin_rub": "100",
        "prior_evaluation_date": prior_evaluation_date,
        "prior_simulated_arrival_lead_time_days": "5",
        "prior_forecast_rate_sales": "1",
        "prior_recommended_order_qty": "0",
        "prior_min_stock_qty": "2",
        "prior_inventory_position_qty": "3",
        "prior_model_stock_qty": "1",
        "prior_reserve_qty": "0",
        "prior_effective_model_pipeline_qty": "2",
        "model_pipeline_qty": "2",
        "current_lead_time_p75_days": "8",
    }


def _decision(
    decision_date: date,
    *,
    code: str = "SKU-1",
    lead_days: str = "5",
    forecast: str = "1",
    order_qty: str = "0",
) -> dict[str, str]:
    return {
        "decision_date": decision_date.isoformat(),
        "nomenclature_code": code,
        "simulated_arrival_lead_time_days": lead_days,
        "forecast_rate_sales": forecast,
        "recommended_order_qty": order_qty,
        "min_stock_qty": "2",
        "max_stock_qty": "6",
        "model_stock_qty": "1",
        "reserve_qty": "0",
        "model_pipeline_qty": "2",
        "effective_model_pipeline_qty": "2",
        "inventory_position_qty": "3",
    }


def test_loss_days_are_grouped_into_consecutive_sku_episodes() -> None:
    rows = [
        _loss_row(date(2026, 3, 10), lost_qty="1"),
        _loss_row(date(2026, 3, 11), lost_qty="2"),
        _loss_row(date(2026, 3, 13), lost_qty="4"),
    ]

    episodes = group_loss_episodes(
        rows,
        decision_rows=[_decision(date(2026, 3, 5))],
    )

    assert len(episodes) == 2
    assert episodes[0]["episode_start"] == "2026-03-10"
    assert episodes[0]["episode_end"] == "2026-03-11"
    assert episodes[0]["lost_observed_qty"] == "3"
    assert episodes[1]["episode_start"] == "2026-03-13"
    assert sum(Decimal(row["lost_observed_qty"]) for row in episodes) == Decimal("7")


def test_arrival_on_first_loss_date_is_an_eligible_advance_decision() -> None:
    episodes = group_loss_episodes(
        [_loss_row(date(2026, 3, 10))],
        decision_rows=[_decision(date(2026, 3, 5), order_qty="2")],
    )

    assert episodes[0]["decision_arrival_date"] == "2026-03-10"
    assert episodes[0]["recoverability"] == "ordered_but_target_too_low"


def test_future_decision_cannot_change_episode_recoverability() -> None:
    episodes = group_loss_episodes(
        [_loss_row(date(2026, 3, 10))],
        decision_rows=[
            _decision(date(2026, 3, 5), forecast="1", order_qty="0"),
            _decision(date(2026, 3, 11), forecast="100", order_qty="100"),
        ],
        sales_by_code={
            "SKU-1": {
                date(2026, 3, 6): Decimal("2"),
                date(2026, 3, 10): Decimal("1"),
                date(2026, 3, 11): Decimal("100"),
            }
        },
    )

    assert episodes[0]["decision_date"] == "2026-03-05"
    assert episodes[0]["recoverability"] == "pipeline_blocked_at_last_chance"
    assert episodes[0]["observed_demand_after_decision_to_first_loss_qty"] == "3"
    assert episodes[0]["forecast_demand_after_decision_to_first_loss_qty"] == "5"


def test_missing_advance_signal_is_not_labeled_as_model_refusal() -> None:
    episodes = group_loss_episodes(
        [_loss_row(date(2026, 3, 10))],
        decision_rows=[_decision(date(2026, 3, 5), forecast="0", order_qty="0")],
    )

    assert episodes[0]["recoverability"] == "no_advance_signal"
