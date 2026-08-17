from decimal import Decimal

from tasks.build_display_auto_order_economic_report import (
    _aggregate_rows,
    _build_period_rows,
    _family,
)


def test_family_classifies_main_display_brands() -> None:
    assert _family("Дисплей Apple iPhone 15 Pro Max") == "Apple"
    assert _family("Дисплей Redmi Note 13") == "Xiaomi / Redmi / Poco"
    assert _family("Дисплей Infinix Hot 40") == "Tecno / Infinix / Itel"


def test_weekly_summary_keeps_strategy_grain_and_averages_capital() -> None:
    rows = [
        {
            "strategy": "actual",
            "business_date": "2026-02-02",
            "served_qty": "10",
            "lost_qty": "1",
            "ending_stock_value_rub": "100",
            "ending_stock_qty": "5",
            "stockout_demand_sku_days": "2",
        },
        {
            "strategy": "actual",
            "business_date": "2026-02-03",
            "served_qty": "20",
            "lost_qty": "2",
            "ending_stock_value_rub": "300",
            "ending_stock_qty": "7",
            "stockout_demand_sku_days": "3",
        },
        {
            "strategy": "model",
            "business_date": "2026-02-02",
            "served_qty": "8",
            "lost_qty": "3",
            "ending_stock_value_rub": "80",
            "ending_stock_qty": "4",
            "stockout_demand_sku_days": "4",
        },
        {
            "strategy": "model",
            "business_date": "2026-02-03",
            "served_qty": "15",
            "lost_qty": "7",
            "ending_stock_value_rub": "120",
            "ending_stock_qty": "6",
            "stockout_demand_sku_days": "5",
        },
    ]

    result = _build_period_rows(rows, period="week")

    assert result == [
        {
            "week": "2026-02-02",
            "actual_served_qty": Decimal("30"),
            "actual_lost_qty": Decimal("3"),
            "actual_average_inventory_value_rub": Decimal("200"),
            "actual_average_inventory_qty": Decimal("6"),
            "actual_stockout_sku_days": 5,
            "model_served_qty": Decimal("23"),
            "model_lost_qty": Decimal("10"),
            "model_average_inventory_value_rub": Decimal("100"),
            "model_average_inventory_qty": Decimal("5"),
            "model_stockout_sku_days": 9,
            "served_qty_delta": Decimal("-7"),
            "capital_delta_rub": Decimal("-100"),
        }
    ]


def test_aggregate_rows_preserves_signed_profit_and_capital_effects() -> None:
    rows = [
        {
            "classification": "safe",
            "gross_profit_delta_model_minus_actual_rub": "10",
            "capital_delta_model_minus_actual_rub": "-30",
            "incremental_sales_qty": "1",
            "model_lost_observed_qty": "0",
            "model_served_hidden_qty": "1",
        },
        {
            "classification": "safe",
            "gross_profit_delta_model_minus_actual_rub": "5",
            "capital_delta_model_minus_actual_rub": "-20",
            "incremental_sales_qty": "2",
            "model_lost_observed_qty": "0.5",
            "model_served_hidden_qty": "2.5",
        },
    ]

    result = _aggregate_rows(rows, group_field="classification")

    assert result[0]["sku_count"] == 2
    assert result[0]["gross_profit_delta_rub"] == Decimal("15")
    assert result[0]["capital_delta_rub"] == Decimal("-50")
    assert result[0]["sales_delta_qty"] == Decimal("3")
