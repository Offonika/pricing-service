from decimal import Decimal

from tasks.evaluate_assortment_lifecycle_exit_duration_grid import combine_grid_results


def _metrics(value: str) -> dict[str, str]:
    return {
        "served_sales_qty": value,
        "gross_profit_rub": value,
        "average_inventory_value_rub": value,
        "carrying_cost_rub": value,
        "economic_effect_rub": value,
        "gmroi": value,
        "ending_inventory_qty": value,
        "ending_target_stock_qty": value,
        "ending_excess_stock_qty": value,
    }


def test_combine_grid_results_orders_each_duration_against_same_base() -> None:
    levels = (
        "brand_quality_construction",
        "quality_construction",
        "quality",
        "all_displays",
    )
    reused = []
    simulated = {"x1.2_exit_d3": {}, "x1.2_exit_d5": {}}
    for level in levels:
        reused.append({"policy": "x1.2_base", "comparable_group_level": level, **_metrics("10")})
        reused.append({"policy": "x1.2_exit_d7", "comparable_group_level": level, **_metrics("13")})
        simulated["x1.2_exit_d3"][level] = {
            key: Decimal(value) + 1 for key, value in _metrics("10").items()
        }
        simulated["x1.2_exit_d5"][level] = {
            key: Decimal(value) + 2 for key, value in _metrics("10").items()
        }

    rows = combine_grid_results(reused_rows=reused, simulated=simulated)

    assert len(rows) == 16
    first = rows[:4]
    assert [row["policy"] for row in first] == [
        "x1.2_base",
        "x1.2_exit_d3",
        "x1.2_exit_d5",
        "x1.2_exit_d7",
    ]
    assert first[1]["vs_base_served_sales_qty_delta"] == "1"
    assert first[2]["vs_base_served_sales_qty_delta"] == "2"
