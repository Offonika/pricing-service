from decimal import Decimal

from tasks.evaluate_assortment_lifecycle_entry_exit_2x2_grid import (
    DECISION_METRICS,
    combine_2x2_results,
    reconstruct_candidate_metrics,
)


def _metrics(value: str) -> dict[str, str]:
    return {metric: value for metric in DECISION_METRICS}


def test_reconstruct_candidate_metrics_derives_capital_without_holdout() -> None:
    baseline = _metrics("10")
    deltas = {
        "served_sales_delta_qty": "1",
        "gross_profit_delta_rub": "2",
        "economic_effect_delta_rub": "1",
        "gmroi_delta": "0.1",
        "ending_excess_stock_delta_qty": "3",
    }

    metrics = reconstruct_candidate_metrics(baseline=baseline, candidate_deltas=deltas)

    assert metrics["served_sales_qty"] == Decimal("11")
    assert metrics["gross_profit_rub"] == Decimal("12")
    assert metrics["economic_effect_rub"] == Decimal("11")
    assert metrics["gmroi"] == Decimal("10.1")
    assert metrics["ending_excess_stock_qty"] == Decimal("13")
    assert metrics["average_inventory_value_rub"] > Decimal("10")


def test_combine_2x2_results_uses_both_entry_baselines() -> None:
    levels = (
        "brand_quality_construction",
        "quality_construction",
        "quality",
        "all_displays",
    )
    x1_2_rows = []
    x1_5_base = {}
    x1_5_d3 = {}
    for level in levels:
        x1_2_rows.extend(
            [
                {"policy": "x1.2_base", "comparable_group_level": level, **_metrics("10")},
                {
                    "policy": "x1.2_exit_d3",
                    "comparable_group_level": level,
                    **_metrics("11"),
                },
            ]
        )
        x1_5_base[level] = _metrics("20")
        x1_5_d3[level] = _metrics("22")

    rows = combine_2x2_results(
        x1_2_rows=x1_2_rows,
        x1_5_base=x1_5_base,
        x1_5_d3=x1_5_d3,
    )

    assert len(rows) == 16
    first = rows[:4]
    assert [row["policy"] for row in first] == [
        "x1.2_base",
        "x1.2_exit_d3",
        "x1.5_base",
        "x1.5_exit_d3",
    ]
    assert first[1]["vs_x1.2_base_served_sales_qty_delta"] == "1"
    assert first[3]["vs_x1.5_base_served_sales_qty_delta"] == "2"
