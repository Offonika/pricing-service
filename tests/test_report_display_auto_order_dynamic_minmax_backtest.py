from decimal import Decimal

from app.services.assortment_lifecycle import AssortmentStatus
from tasks.report_display_auto_order_dynamic_minmax_backtest import (
    PROFILES,
    _dynamic_scenario,
    _select_profile,
    _shadow_rows,
)
from tasks.report_display_auto_order_frozen_backtest import CarryingCostScenario, FrozenScenario


def _source_scenario() -> FrozenScenario:
    return FrozenScenario(
        scenario_id="source",
        stage_profile="typical",
        kmp4_weight=Decimal("0.5"),
        cost=CarryingCostScenario(
            name="base",
            capital_annual_rate=Decimal("0.3"),
            storage_annual_rate=Decimal("0.1"),
            obsolescence_annual_rate=Decimal("0.25"),
        ),
        base_pipeline_profile="test",
        base_pipeline_medium_fraction=Decimal("0.95"),
        base_pipeline_low_fraction=Decimal("0.9"),
        base_pipeline_lot_risk_boundary="p75",
        base_pipeline_lot_risk_fraction=Decimal("0.9"),
    )


def test_dynamic_profile_uses_shortage_not_general_acceleration_buffer() -> None:
    scenario = _dynamic_scenario(_source_scenario(), PROFILES[0])

    assert scenario.grow_acceleration_quantity_policy == "dynamic_minmax_shortage"
    assert scenario.grow_acceleration_cap_to_projected_shortage is True
    assert scenario.grow_acceleration_single_open_lot is True
    assert scenario.grow_acceleration_require_forecast_growth is True
    assert scenario.grow_acceleration_stage_budget_rub > Decimal("1000000000")


def test_selection_never_uses_july_for_profile_choice() -> None:
    rows = []
    for profile, pre, july in zip(
        PROFILES,
        ("10", "20", "15"),
        ("1000", "-1", "900"),
        strict=True,
    ):
        rows.extend(
            [
                {
                    "scenario_role": profile.role,
                    "period": "pre_july",
                    "economic_contribution_delta_rub": pre,
                    "served_observed_delta_qty": "1",
                },
                {
                    "scenario_role": profile.role,
                    "period": "july",
                    "economic_contribution_delta_rub": july,
                    "served_observed_delta_qty": "1",
                },
            ]
        )

    selected = _select_profile(rows)

    assert selected["scenario_role"] == "dynamic_balanced_p50"
    assert selected["july_economic_contribution_delta_rub"] == "-1"
    assert selected["positive_on_holdout"] is False


def test_shadow_rows_include_only_positive_selected_role_and_never_apply() -> None:
    base = {
        "decision_date": "2026-06-01",
        "nomenclature_code": "SKU-1",
        "status": AssortmentStatus.SALE.value,
        "acceleration_static_min_stock_qty": "10",
        "acceleration_free_stock_qty": "12",
        "acceleration_recent_sales_qty": "4",
        "acceleration_baseline_sales_qty": "1",
        "acceleration_recent_rate": "0.57",
        "acceleration_baseline_rate": "0.04",
        "acceleration_lead_quantile": "p50",
        "acceleration_projected_demand_to_p75_qty": "20",
        "acceleration_guard_inventory_position_qty": "15",
        "acceleration_projected_shortage_to_p75_qty": "5",
    }
    rows = _shadow_rows(
        [
            {
                **base,
                "scenario_role": "dynamic_fast_p50",
                "acceleration_order_component_qty": "5",
            },
            {
                **base,
                "scenario_role": "dynamic_balanced_p50",
                "acceleration_order_component_qty": "3",
            },
            {
                **base,
                "scenario_role": "dynamic_fast_p50",
                "nomenclature_code": "SKU-2",
                "acceleration_order_component_qty": "0",
            },
        ],
        selected_role="dynamic_fast_p50",
        names={"SKU-1": "Товар"},
    )

    assert len(rows) == 1
    assert rows[0]["dynamic_minmax_increment_qty"] == "5"
    assert rows[0]["production_action"] == "none_read_only"
    assert rows[0]["reason_code"] == "stock_above_min_accelerating_shortage"
