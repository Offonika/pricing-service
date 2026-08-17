from datetime import date, timedelta
from decimal import Decimal

from tasks.report_display_auto_order_economic_backtest import (
    SkuEconomics,
    _potential_demand,
    _winner,
    build_hidden_demand_base,
)


def test_sku_economics_uses_return_adjusted_unit_margin() -> None:
    economics = SkuEconomics(
        gross_sale_qty=Decimal("10"),
        return_qty=Decimal("-2"),
        net_revenue_rub=Decimal("800"),
        net_cost_rub=Decimal("500"),
        gross_sale_cost_rub=Decimal("700"),
    )

    assert economics.net_revenue_per_gross_unit == Decimal("80")
    assert economics.net_cost_per_gross_unit == Decimal("50")
    assert economics.gross_profit_per_gross_unit == Decimal("30")
    assert economics.inventory_cost_per_unit == Decimal("70")


def test_potential_demand_scales_only_hidden_component() -> None:
    assert _potential_demand(
        observed=Decimal("4"),
        hidden_base=Decimal("2"),
        demand_factor=Decimal("0.5"),
    ) == Decimal("5")


def test_hidden_demand_uses_only_past_sales_on_actual_stockout_day() -> None:
    stockout_day = date(2026, 2, 1)
    history_from = stockout_day - timedelta(days=180)
    sales = {history_from + timedelta(days=offset): Decimal("1") for offset in range(180)}
    availability = set(sales)
    sales[stockout_day + timedelta(days=1)] = Decimal("10000")

    result = build_hidden_demand_base(
        codes=["SKU-1"],
        sales_by_code={"SKU-1": sales},
        availability_by_code={"SKU-1": availability},
        actual_stock_by_day={stockout_day: {"SKU-1": Decimal("0")}},
        date_from=stockout_day,
        date_to=stockout_day,
    )

    assert result["SKU-1"][stockout_day] == Decimal("1")


def test_winner_does_not_use_human_orders_as_ground_truth() -> None:
    actual = {
        "gross_profit_rub": "100",
        "average_inventory_value_rub": "200",
        "gmroi_annualized": "1",
    }
    model = {
        "gross_profit_rub": "110",
        "average_inventory_value_rub": "150",
        "gmroi_annualized": "1.5",
    }

    assert _winner(actual, model) == "model_dominates"
