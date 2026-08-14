from datetime import date, timedelta
from decimal import Decimal

from tasks.report_display_auto_order_working_safety_backtest import (
    ComparableGroupFallback,
    _merge_decision_rows,
    _normalize_evaluation_economics,
    _sales_overlap_mismatches,
    _variants_for_experiment,
)


def test_group_fallback_uses_only_errors_completed_before_as_of() -> None:
    decision_day = date(2025, 10, 1)
    rows = []
    sales = {}
    group_keys = {}
    for index in range(8):
        code = f"SKU-{index}"
        rows.append(
            {
                "decision_date": decision_day.isoformat(),
                "nomenclature_code": code,
                "scheduled_review": "1",
                "lead_time_p50_days": "1",
                "forecast_rate_sales": "0",
            }
        )
        sales[code] = {decision_day + timedelta(days=2): Decimal("1")}
        group_keys[code] = ("brand", "quality-construction", "quality", "all")

    fallback = ComparableGroupFallback(
        decision_rows_by_date={decision_day: rows},
        sales_by_code=sales,
        group_keys_by_code=group_keys,
        group_level="quality_construction",
        order_cadence_days=1,
        lookback_days=365,
        minimum_group_size=8,
    )

    assert (
        fallback.samples(
            as_of=decision_day + timedelta(days=2),
            group_key="quality-construction",
        )
        == ()
    )
    assert fallback.own_samples(as_of=decision_day + timedelta(days=2), code="SKU-0") == ()
    assert (
        fallback.samples(
            as_of=decision_day + timedelta(days=3),
            group_key="quality-construction",
        )
        == (Decimal("1"),) * 8
    )
    assert fallback.own_samples(as_of=decision_day + timedelta(days=3), code="SKU-0") == (
        Decimal("1"),
    )


def test_warmup_merge_rejects_overlap_with_evaluated_decisions() -> None:
    current_from = date(2026, 1, 1)
    current = {current_from: [{"nomenclature_code": "SKU-1"}]}
    warmup = {current_from: [{"nomenclature_code": "SKU-1"}]}

    try:
        _merge_decision_rows(current, warmup, current_from=current_from)
    except ValueError as exc:
        assert "overlap" in str(exc)
    else:
        raise AssertionError("overlapping warm-up decisions must be rejected")


def test_sales_overlap_reconciliation_reports_changed_quantity() -> None:
    business_date = date(2025, 10, 1)
    mismatches = _sales_overlap_mismatches(
        {"SKU-1": {business_date: Decimal("2")}},
        {"SKU-1": {business_date: Decimal("1")}},
        date_from=business_date,
        date_to=business_date,
    )

    assert mismatches == [("SKU-1", business_date, Decimal("2"), Decimal("1"))]


def test_economic_comparison_uses_one_common_evaluation_rate() -> None:
    metrics = {
        "average_inventory_value_rub": "3650",
        "gross_profit_rub": "1000",
        "carrying_cost_rub": "0",
        "economic_effect_rub": "1000",
    }

    _normalize_evaluation_economics(
        metrics,
        annual_rate=Decimal("0.65"),
        period_days=10,
    )

    assert metrics["carrying_cost_rub"] == "65.00"
    assert metrics["economic_effect_rub"] == "935.00"


def test_targeted_variant_grid_is_bounded() -> None:
    variants = _variants_for_experiment("targeted")
    challengers = [row for row in variants if str(row["variant_id"]).startswith("targeted_")]

    assert len(variants) == 11
    assert len(challengers) == 9
    assert {row["unit_cap"] for row in challengers} == {1, 2, 3}
    assert {row["hurdle_multiplier"] for row in challengers} == {
        Decimal("1.25"),
        Decimal("1.5"),
        Decimal("2.0"),
    }
    assert all(row["require_shortage"] for row in challengers)
    assert all(row["single_open_lot"] for row in challengers)
    assert all(row["min_sales_days"] == 2 for row in challengers)
