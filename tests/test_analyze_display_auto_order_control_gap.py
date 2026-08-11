from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from tasks.analyze_display_auto_order_control_gap import (
    classify_loss_reason,
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
