from datetime import date
from decimal import Decimal

from app.services.display_family_demand import build_display_family_members
from tasks.report_display_family_demand_backtest import apply_family_forecast_allocation


def _members():
    return build_display_family_members(
        [
            {
                "nomenclature_code": "OLD",
                "name": "Дисплей iPhone 14 Pro Max Soft OLED JK",
                "model_tokens": ("apple:model:iphone 14 pro max",),
            },
            {
                "nomenclature_code": "NEW",
                "name": "Дисплей iPhone 14 Pro Max Soft OLED GX",
                "model_tokens": ("apple:model:iphone 14 pro max",),
            },
        ]
    )


def _row(code: str, rate: str) -> dict[str, str]:
    return {
        "nomenclature_code": code,
        "name": code,
        "forecast_rate_sales": rate,
    }


def test_family_transform_does_not_use_current_day_sales() -> None:
    decision_date = date(2026, 2, 10)
    transformed, _audit = apply_family_forecast_allocation(
        decision_rows_by_date={decision_date: [_row("OLD", "1"), _row("NEW", "1")]},
        sales_by_code={"OLD": {decision_date: Decimal("10")}},
        members=_members(),
        first_seen={"OLD": decision_date, "NEW": decision_date},
        lookback_days=14,
        blend=Decimal("1"),
    )

    assert [row["forecast_rate_sales"] for row in transformed[decision_date]] == ["1", "1"]


def test_family_transform_preserves_daily_family_forecast_total() -> None:
    decision_date = date(2026, 2, 10)
    transformed, _audit = apply_family_forecast_allocation(
        decision_rows_by_date={decision_date: [_row("OLD", "1"), _row("NEW", "3")]},
        sales_by_code={
            "OLD": {date(2026, 2, 9): Decimal("9")},
            "NEW": {date(2026, 2, 9): Decimal("1")},
        },
        members=_members(),
        first_seen={"OLD": decision_date, "NEW": decision_date},
        lookback_days=14,
        blend=Decimal("1"),
    )

    assert sum(
        (Decimal(row["forecast_rate_sales"]) for row in transformed[decision_date]),
        Decimal("0"),
    ) == Decimal("4")


def test_future_sku_does_not_change_past_family_allocation() -> None:
    past = date(2026, 2, 10)
    future = date(2026, 3, 1)
    transformed, _audit = apply_family_forecast_allocation(
        decision_rows_by_date={
            past: [_row("OLD", "2")],
            future: [_row("OLD", "2"), _row("NEW", "8")],
        },
        sales_by_code={"NEW": {date(2026, 2, 9): Decimal("100")}},
        members=_members(),
        first_seen={"OLD": past, "NEW": future},
        lookback_days=14,
        blend=Decimal("1"),
    )

    assert transformed[past][0]["forecast_rate_sales"] == "2"
