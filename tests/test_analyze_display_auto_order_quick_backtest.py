from datetime import date, timedelta
from decimal import Decimal

from tasks.analyze_display_auto_order_quick_backtest import (
    _acceleration_by_sku,
    _build_markdown,
    _mechanism_rows,
    classify_demand_pattern,
    completed_weekly_demand,
    croston_sba_forecast,
    empirical_quantile,
    tsb_forecast,
)


def test_classify_demand_pattern_covers_standard_quadrants() -> None:
    smooth = classify_demand_pattern([Decimal("1")] * 10)
    intermittent = classify_demand_pattern([Decimal("0"), Decimal("1")] * 5)
    erratic = classify_demand_pattern([Decimal("1"), Decimal("6")] * 5)
    lumpy = classify_demand_pattern([Decimal("0"), Decimal("1"), Decimal("0"), Decimal("6")] * 3)

    assert smooth.name == "smooth"
    assert intermittent.name == "intermittent"
    assert erratic.name == "erratic"
    assert lumpy.name == "lumpy"


def test_classify_demand_pattern_labels_sparse_history() -> None:
    assert classify_demand_pattern([Decimal("0")] * 10).name == "no_history"
    assert (
        classify_demand_pattern([Decimal("0")] * 9 + [Decimal("2")]).name == "insufficient_history"
    )


def test_completed_weekly_demand_excludes_current_and_future_sales() -> None:
    as_of = date(2026, 3, 1)
    sales = {
        as_of - timedelta(days=1): Decimal("2"),
        as_of: Decimal("100"),
        as_of + timedelta(days=1): Decimal("1000"),
    }

    weekly = completed_weekly_demand(sales, as_of=as_of, weeks=2)

    assert sum(weekly, Decimal("0")) == Decimal("2")


def test_intermittent_forecasts_are_non_negative_and_causal() -> None:
    history = [Decimal("0"), Decimal("2"), Decimal("0"), Decimal("0")] * 5

    assert croston_sba_forecast(history) > 0
    assert tsb_forecast(history) > 0
    assert croston_sba_forecast([Decimal("0")] * 10) == 0
    assert tsb_forecast([Decimal("0")] * 10) == 0


def test_empirical_quantile_is_bounded_by_observations() -> None:
    values = [Decimal("0"), Decimal("1"), Decimal("3"), Decimal("7")]

    assert empirical_quantile(values, Decimal("0.75")) == Decimal("3")
    assert empirical_quantile(values, Decimal("0.90")) == Decimal("7")


def test_mechanism_rows_separate_first_and_repeat_orders() -> None:
    rows = [
        {
            "acceleration_order_lines": 0,
            "repeat_acceleration_order_lines": 0,
            "economic_contribution_delta_to_control_rub": "0",
        },
        {
            "acceleration_order_lines": 1,
            "repeat_acceleration_order_lines": 0,
            "economic_contribution_delta_to_control_rub": "-10",
        },
        {
            "acceleration_order_lines": 3,
            "repeat_acceleration_order_lines": 2,
            "economic_contribution_delta_to_control_rub": "-90",
        },
    ]

    result = {row["mechanism_group"]: row for row in _mechanism_rows(rows)}

    assert result["no_acceleration_order"]["sku_count"] == 1
    assert result["first_order_only"]["sku_count"] == 1
    assert result["has_repeat_orders"]["sku_count"] == 1
    assert result["has_repeat_orders"]["economic_contribution_delta_to_control_rub"] == "-90"


def test_acceleration_rows_use_single_open_order_component_when_present() -> None:
    rows = [
        {
            "decision_date": "2026-03-01",
            "nomenclature_code": "SKU-1",
            "acceleration_allocated_qty": "3",
            "acceleration_order_component_qty": "2",
            "recommended_order_qty": "10",
        },
        {
            "decision_date": "2026-03-02",
            "nomenclature_code": "SKU-1",
            "acceleration_allocated_qty": "2",
            "acceleration_order_component_qty": "0",
            "recommended_order_qty": "4",
        },
    ]

    result = _acceleration_by_sku(rows)["SKU-1"]

    assert result["acceleration_order_lines"] == 1
    assert result["first_acceleration_order_qty"] == Decimal("2")
    assert result["repeat_acceleration_order_qty"] == Decimal("0")


def test_segmented_markdown_does_not_claim_positive_challenger_failed() -> None:
    report = _build_markdown(
        {
            "scenario_ids": {"hypothesis": "scenario_segment_low_risk"},
            "headline": {
                "economic_delta_rub": "19050",
                "served_observed_delta_qty": "128",
                "capital_delta_rub": "41933",
                "ending_inventory_delta_qty": "70.5",
            },
            "data_quality": {
                "source_cohort_sku_count": 2662,
                "simulated_sku_count": 2605,
            },
        }
    )

    assert "положительный экономический вклад" in report
    assert "не окупилось" not in report
