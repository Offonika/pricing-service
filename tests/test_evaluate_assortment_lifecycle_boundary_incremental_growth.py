from datetime import date

from tasks.evaluate_assortment_lifecycle_boundary_incremental_growth import (
    meaningful_guardrails,
    summarize_order_pair,
)


def _metrics(*, effect: str, served: str = "10", excess: str = "5") -> dict[str, str]:
    return {
        "served_sales_qty": served,
        "gross_profit_rub": "100",
        "average_inventory_value_rub": "100",
        "carrying_cost_rub": "10",
        "economic_effect_rub": effect,
        "gmroi": "2",
        "ending_inventory_qty": "10",
        "ending_target_stock_qty": "10",
        "ending_excess_stock_qty": excess,
    }


def test_meaningful_guardrails_require_30000_rub_and_all_safety_gates() -> None:
    control = _metrics(effect="100000")
    passing = _metrics(effect="130000")
    below_materiality = _metrics(effect="129999")

    assert all(meaningful_guardrails(passing, control).values())
    assert (
        meaningful_guardrails(below_materiality, control)["economic_effect_gain_at_least_30000_rub"]
        is False
    )


def test_order_pair_reports_only_real_order_reduction_as_actionable() -> None:
    guarded = {
        (date(2026, 2, 1), "A"),
        (date(2026, 2, 2), "B"),
    }
    control = [
        {
            "decision_date": "2026-02-01",
            "nomenclature_code": "A",
            "ordinary_recommended_order_qty": "10",
            "unprotected_min_stock_qty": "8",
            "unprotected_max_stock_qty": "10",
            "min_stock_qty": "13",
            "max_stock_qty": "13",
            "representation_minimum_qty": "13",
            "economic_safety_stock_qty": "2",
            "decision_service_buffer_qty": "0",
            "acceleration_order_component_qty": "0",
        },
        {
            "decision_date": "2026-02-02",
            "nomenclature_code": "B",
            "ordinary_recommended_order_qty": "0",
            "unprotected_min_stock_qty": "14",
            "unprotected_max_stock_qty": "16",
            "min_stock_qty": "14",
            "max_stock_qty": "16",
            "representation_minimum_qty": "13",
            "economic_safety_stock_qty": "1",
            "decision_service_buffer_qty": "0",
            "acceleration_order_component_qty": "0",
        },
    ]
    candidate = [
        {**control[0], "ordinary_recommended_order_qty": "7"},
        control[1],
    ]

    result = summarize_order_pair(
        guarded_keys=guarded,
        control_rows=control,
        candidate_rows=candidate,
    )

    assert result["changed_order_key_count"] == 1
    assert result["reduced_order_qty"] == "3"
    assert result["representation_binding_target_key_count"] == 1
    assert result["growth_protection_increment_qty"] == "0"
