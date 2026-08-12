from decimal import Decimal

from tasks.report_display_auto_order_hybrid_gap_backtest import _delta_row


def test_hybrid_comparison_delta_uses_unchanged_control() -> None:
    columns = {
        "served_qty": "10",
        "served_observed_qty": "8",
        "lost_qty": "2",
        "lost_observed_qty": "2",
        "fill_rate": "0.8",
        "observed_fill_rate": "0.8",
        "gross_profit_rub": "100",
        "average_inventory_value_rub": "50",
        "economic_contribution_rub": "90",
        "gmroi_annualized": "2",
        "ending_inventory_qty": "4",
        "order_qty": "12",
        "order_value_rub": "500",
    }
    candidate = dict(columns)
    candidate.update(
        {
            "served_qty": "12",
            "served_observed_qty": "10",
            "gross_profit_rub": "125",
            "average_inventory_value_rub": "55",
        }
    )

    row = _delta_row(candidate, columns, scenario_role="hybrid_p50")

    assert Decimal(row["delta_to_control_served_observed_qty"]) == Decimal("2")
    assert Decimal(row["delta_to_control_gross_profit_rub"]) == Decimal("25")
    assert Decimal(row["delta_to_control_average_inventory_value_rub"]) == Decimal("5")
