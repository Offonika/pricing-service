from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

from tasks.report_display_auto_order_working_safety_backtest import (
    ComparableGroupFallback,
    _isolated_sample_cache,
    _merge_decision_rows,
    _metric_reconciliation,
    _normalize_evaluation_economics,
    _sales_overlap_mismatches,
    _selected_candidate_audit,
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


def test_scenario_sample_cache_is_isolated_from_other_variants() -> None:
    key = ("SKU-1", date(2026, 2, 1), 30)
    shared = {key: [Decimal("1")]}

    isolated = _isolated_sample_cache(shared)
    isolated[key].append(Decimal("2"))
    isolated[("SKU-2", date(2026, 2, 1), 30)] = [Decimal("3")]

    assert shared == {key: [Decimal("1")]}


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
    assert all(row["history"] == "current" for row in challengers)
    assert all(not row["fallback"] for row in challengers)
    assert all(row["working_history"] == "extended" for row in challengers)
    assert all(row["working_fallback"] for row in challengers)


def test_selected_candidate_audit_attributes_incremental_ending_excess() -> None:
    def metric(*, inventory: str, target: str, served: str, orders: str) -> SimpleNamespace:
        return SimpleNamespace(
            ending_inventory_qty=Decimal(inventory),
            ending_target_stock_qty=Decimal(target),
            safety_stock_units_ordered=Decimal("2"),
            served_observed_qty=Decimal(served),
            lost_observed_qty=Decimal("1"),
            gross_profit_rub=Decimal("100"),
            order_qty=Decimal(orders),
            order_value_rub=Decimal("500"),
        )

    baseline = SimpleNamespace(
        model={"SKU-1": metric(inventory="3", target="2", served="5", orders="1")}
    )
    selected = SimpleNamespace(
        model={"SKU-1": metric(inventory="6", target="3", served="7", orders="3")},
        decision_rows=[
            {
                "decision_date": "2026-06-01",
                "nomenclature_code": "SKU-1",
                "status": "working",
                "economic_safety_stock_qty": "2",
                "recommended_order_qty": "2",
                "safety_sample_source": "own",
                "working_safety_projected_shortage_qty": "2",
                "working_safety_blocker": "",
            }
        ],
    )

    rows, diagnostics = _selected_candidate_audit(
        selected,
        baseline_result=baseline,
        period_from=date(2026, 2, 1),
        period_to=date(2026, 6, 30),
        final_status_by_code={"SKU-1": "working"},
        name_by_code={"SKU-1": "Test display"},
    )

    assert rows[0]["name"] == "Test display"
    assert rows[0]["ending_excess_stock_qty_baseline"] == "1"
    assert rows[0]["ending_excess_stock_qty_selected"] == "3"
    assert rows[0]["ending_excess_stock_qty_delta"] == "2"
    assert rows[0]["simulation_served_sales_qty_delta"] == "2"
    assert diagnostics["ending_excess_stock_qty_delta"] == "2"
    assert diagnostics["positive_ending_excess_delta_sku_count"] == 1
    assert diagnostics["positive_excess_driver_breakdown"]["inventory_up"] == {
        "sku_count": 1,
        "ending_excess_delta_qty": "2",
    }
    assert diagnostics["common_reference_target"]["ending_excess_stock_qty_delta"] == "3"


def test_metric_reconciliation_rejects_nonzero_delta() -> None:
    expected = {
        "observed_sales_qty": "10",
        "served_sales_qty": "9",
        "lost_sales_qty": "1",
        "gross_profit_rub": "100",
        "average_inventory_value_rub": "50",
        "carrying_cost_rub": "5",
        "economic_effect_rub": "95",
        "gmroi": "2",
        "ending_inventory_qty": "3",
        "ending_excess_stock_qty": "1",
        "recommended_order_qty": "2",
        "order_line_count": "1",
        "ordered_safety_stock_qty": "1",
    }
    recalculated = dict(expected)
    recalculated["ending_excess_stock_qty"] = "2"

    reconciliation = _metric_reconciliation(recalculated, expected)

    assert reconciliation["passed"] is False
    assert reconciliation["deltas"]["ending_excess_stock_qty"] == "1"
