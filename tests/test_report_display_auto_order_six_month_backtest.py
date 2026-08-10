from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from tasks.build_display_auto_order_dry_run import load_auto_order_policy
from tasks.report_display_auto_order_six_month_backtest import (
    LaunchProfile,
    PipelineLot,
    PurchaseLine,
    ReceiptLine,
    actual_purchase_summary,
    actual_stock_summary,
    build_launch_observations,
    build_launch_profile_snapshot,
    forecast_rate,
    historical_lifecycle_decision,
    initial_pipeline,
    item_active_as_of,
    run_simulation,
    select_launch_profile,
    stage_model_scenario,
    stage_recommendation,
)

POLICY_PATH = Path("config/assortment/display-auto-order-policy.json")


def _item() -> dict[str, object]:
    return {
        "nomenclature_code": "SKU-1",
        "name": "Дисплей тестовый",
        "status": "working",
        "auto_order_allowed": True,
    }


def _sales(date_from: date, date_to: date, qty: str = "1") -> dict[date, Decimal]:
    return {
        date_from + timedelta(days=offset): Decimal(qty)
        for offset in range((date_to - date_from).days + 1)
    }


def test_forecast_rate_does_not_use_future_sales() -> None:
    as_of = date(2026, 2, 1)
    history_from = as_of - timedelta(days=179)
    availability = set(_sales(history_from, as_of))
    baseline = _sales(history_from, as_of)
    with_future_spike = {**baseline, as_of + timedelta(days=1): Decimal("10000")}

    baseline_rate, baseline_trend, baseline_evidence = forecast_rate(
        baseline,
        availability,
        as_of=as_of,
    )
    spike_rate, spike_trend, spike_evidence = forecast_rate(
        with_future_spike,
        availability,
        as_of=as_of,
    )

    assert spike_rate == baseline_rate
    assert spike_trend == baseline_trend
    assert spike_evidence == baseline_evidence


def test_initial_pipeline_fifo_deducts_receipts_before_start() -> None:
    purchases = {
        "SKU-1": [
            PurchaseLine(
                created_at=date(2026, 1, 1),
                qty=Decimal("10"),
                price=Decimal("100"),
                supplier_name="Supplier",
                order_ref="PO-1",
                expected_receipt_at=date(2026, 2, 10),
            )
        ]
    }
    receipts = {"SKU-1": [ReceiptLine(received_at=date(2026, 1, 20), qty=Decimal("6"))]}

    result = initial_pipeline(
        purchases,
        receipts,
        as_of=date(2026, 1, 31),
        fallback_lead_time_days=52,
    )

    assert len(result["SKU-1"]) == 1
    assert result["SKU-1"][0].qty == Decimal("4")
    assert result["SKU-1"][0].arrival_at == date(2026, 2, 10)


def test_actual_purchase_summary_uses_same_cohort() -> None:
    purchases = {
        code: [
            PurchaseLine(
                created_at=date(2026, 2, 1),
                qty=Decimal(qty),
                price=Decimal("100"),
                supplier_name="Supplier",
                order_ref=f"PO-{code}",
                expected_receipt_at=None,
            )
        ]
        for code, qty in (("SKU-1", "2"), ("OUTSIDE", "50"))
    }

    summary = actual_purchase_summary(
        purchases,
        date_from=date(2026, 2, 1),
        date_to=date(2026, 7, 31),
        allowed_codes={"SKU-1"},
    )

    assert summary["line_count"] == 1
    assert summary["qty"] == "2"
    assert summary["value_rub"] == "200"


def test_stockout_days_ignore_sku_without_demand_signal() -> None:
    result = actual_stock_summary(
        {
            date(2026, 2, 1): {"SELLING": Decimal("0"), "NO-DEMAND": Decimal("0")},
            date(2026, 2, 2): {"SELLING": Decimal("0"), "NO-DEMAND": Decimal("0")},
        },
        {"SELLING": {date(2026, 1, 31): Decimal("1")}, "NO-DEMAND": {}},
        codes=["SELLING", "NO-DEMAND"],
        date_from=date(2026, 2, 1),
        date_to=date(2026, 2, 2),
    )

    assert result["demand_active_sku_days"] == 2
    assert result["stockout_sku_days"] == 2


def test_simulated_pipeline_prevents_weekly_duplicate_before_arrival() -> None:
    policy = load_auto_order_policy(POLICY_PATH)
    simulation_from = date(2026, 2, 1)
    simulation_to = date(2026, 3, 7)
    history_from = simulation_from - timedelta(days=179)
    sales = _sales(history_from, simulation_to)
    availability = set(sales)

    result = run_simulation(
        items=[_item()],
        sales_by_code={"SKU-1": sales},
        availability_by_code={"SKU-1": availability},
        actual_stock_by_day={simulation_from: {"SKU-1": Decimal("0")}},
        purchase_history={},
        initial_pipeline_by_code={},
        policy=policy,
        date_from=simulation_from,
        date_to=simulation_to,
        lead_time_days=52,
        scenario="test",
    )

    scheduled = [
        row for row in result.decision_rows if Decimal(str(row["scheduled_order_qty"])) > 0
    ]
    assert len(scheduled) == 1
    assert scheduled[0]["decision_date"] == "2026-02-01"
    assert result.summary.order_lines == 1


def test_simulation_starts_from_prior_day_closing_stock_when_available() -> None:
    policy = load_auto_order_policy(POLICY_PATH)
    simulation_day = date(2026, 2, 1)
    prior_day = simulation_day - timedelta(days=1)

    result = run_simulation(
        items=[_item()],
        sales_by_code={"SKU-1": {}},
        availability_by_code={"SKU-1": set()},
        actual_stock_by_day={
            prior_day: {"SKU-1": Decimal("10")},
            simulation_day: {"SKU-1": Decimal("7")},
        },
        purchase_history={},
        initial_pipeline_by_code={},
        policy=policy,
        date_from=simulation_day,
        date_to=simulation_day,
        lead_time_days=52,
        scenario="test",
    )

    assert result.ending_stock_by_code["SKU-1"] == Decimal("10")


def test_simulated_arrival_changes_later_order_state() -> None:
    policy = load_auto_order_policy(POLICY_PATH)
    simulation_from = date(2026, 2, 1)
    simulation_to = date(2026, 2, 22)
    history_from = simulation_from - timedelta(days=179)
    sales = _sales(history_from, simulation_to, qty="0")
    sales[simulation_from - timedelta(days=1)] = Decimal("30")

    result = run_simulation(
        items=[_item()],
        sales_by_code={"SKU-1": sales},
        availability_by_code={"SKU-1": set(sales)},
        actual_stock_by_day={simulation_from: {"SKU-1": Decimal("0")}},
        purchase_history={},
        initial_pipeline_by_code={
            "SKU-1": [
                PipelineLot(
                    arrival_at=date(2026, 2, 5),
                    qty=Decimal("100"),
                    source="test",
                )
            ]
        },
        policy=policy,
        date_from=simulation_from,
        date_to=simulation_to,
        lead_time_days=52,
        scenario="test",
    )

    feb_8 = next(row for row in result.decision_rows if row["decision_date"] == "2026-02-08")
    assert Decimal(str(feb_8["model_free_stock"])) == Decimal("100")
    assert Decimal(str(feb_8["recommended_order_qty"])) == Decimal("0")


def test_monthly_summary_keeps_calendar_boundaries() -> None:
    policy = load_auto_order_policy(POLICY_PATH)
    simulation_from = date(2026, 2, 27)
    simulation_to = date(2026, 3, 3)
    history_from = simulation_from - timedelta(days=179)
    sales = _sales(history_from, simulation_to, qty="0")

    result = run_simulation(
        items=[_item()],
        sales_by_code={"SKU-1": sales},
        availability_by_code={"SKU-1": set(sales)},
        actual_stock_by_day={simulation_from: {"SKU-1": Decimal("1")}},
        purchase_history={},
        initial_pipeline_by_code={},
        policy=policy,
        date_from=simulation_from,
        date_to=simulation_to,
        lead_time_days=52,
        scenario="test",
    )

    assert [row["month"] for row in result.monthly_rows] == ["2026-02", "2026-03"]


def _historical_item() -> dict[str, object]:
    return {
        "nomenclature_code": "SKU-HISTORY",
        "name": "Дисплей исторический",
        "source_record": {"created_at": "2025-01-01"},
    }


def _historical_purchase() -> PurchaseLine:
    return PurchaseLine(
        created_at=date(2025, 1, 2),
        qty=Decimal("5"),
        price=Decimal("100"),
        supplier_name="Supplier",
        order_ref="PO-HISTORY",
        expected_receipt_at=date(2025, 2, 20),
        cargo_handoff_at=date(2025, 1, 5),
    )


def test_historical_lifecycle_reconstructs_four_active_stages() -> None:
    as_of = date(2025, 6, 30)
    purchase = _historical_purchase()
    no_sales, _ = historical_lifecycle_decision(
        item=_historical_item(),
        sales={},
        availability_dates=set(),
        purchases=[purchase],
        receipts=[],
        as_of=as_of,
        previous_status=None,
    )
    first_sale = {as_of: Decimal("1")}
    sales_start, _ = historical_lifecycle_decision(
        item=_historical_item(),
        sales=first_sale,
        availability_dates={as_of},
        purchases=[purchase],
        receipts=[],
        as_of=as_of,
        previous_status=no_sales.status.value,
    )
    proven_sales = {as_of - timedelta(days=offset): Decimal("1") for offset in range(12)}
    growing, _ = historical_lifecycle_decision(
        item=_historical_item(),
        sales=proven_sales,
        availability_dates=set(proven_sales),
        purchases=[purchase],
        receipts=[],
        as_of=as_of,
        previous_status=sales_start.status.value,
    )
    old_item = _historical_item()
    old_item["source_record"] = {
        "created_at": "2024-01-01",
        "first_sale_at": "2024-06-01",
    }
    supporting, _ = historical_lifecycle_decision(
        item=old_item,
        sales={},
        availability_dates=set(),
        purchases=[purchase],
        receipts=[],
        as_of=as_of,
        previous_status=None,
    )

    assert no_sales.status.value == "new_item"
    assert sales_start.status.value == "sales_start"
    assert growing.status.value == "sale"
    assert supporting.status.value == "working"


def test_historical_lifecycle_does_not_use_future_sales() -> None:
    as_of = date(2025, 6, 30)
    baseline, baseline_evidence = historical_lifecycle_decision(
        item=_historical_item(),
        sales={},
        availability_dates=set(),
        purchases=[_historical_purchase()],
        receipts=[],
        as_of=as_of,
        previous_status=None,
    )
    with_future, future_evidence = historical_lifecycle_decision(
        item=_historical_item(),
        sales={as_of + timedelta(days=1): Decimal("1000")},
        availability_dates={as_of + timedelta(days=1)},
        purchases=[_historical_purchase()],
        receipts=[],
        as_of=as_of,
        previous_status=None,
    )

    assert with_future.status == baseline.status
    assert future_evidence == baseline_evidence


def test_stage_recommendation_limits_sales_start() -> None:
    policy = load_auto_order_policy(POLICY_PATH)
    as_of = date(2025, 6, 30)
    sales = {as_of - timedelta(days=offset): Decimal("1") for offset in range(5)}
    lifecycle, evidence = historical_lifecycle_decision(
        item=_historical_item(),
        sales=sales,
        availability_dates=set(sales),
        purchases=[_historical_purchase()],
        receipts=[],
        as_of=as_of,
        previous_status="new_item",
    )
    recommended, target, decision, _tier, _raw = stage_recommendation(
        lifecycle=lifecycle,
        rate=Decimal("1"),
        trend="flat_or_slowing",
        evidence=evidence,
        free_stock=Decimal("0"),
        incoming_qty=Decimal("0"),
        policy=policy,
    )

    assert lifecycle.status.value == "sales_start"
    assert target == Decimal("7")
    assert recommended == Decimal("7")
    assert decision == "manual_review"


def test_launch_profile_snapshot_does_not_use_incomplete_future_window() -> None:
    policy = load_auto_order_policy(POLICY_PATH)
    launch_at = date(2025, 1, 10)
    item = {
        **_historical_item(),
        "brand_compatibility": "Apple",
        "quality_normalized": "soft_oled",
        "price_segment": "premium",
    }
    availability = {launch_at + timedelta(days=offset) for offset in range(30)}
    sales = {business_date: Decimal("1") for business_date in availability}
    observations = build_launch_observations(
        items=[item],
        sales_by_code={"SKU-HISTORY": sales},
        availability_by_code={"SKU-HISTORY": availability},
        receipt_history={},
        history_start=date(2025, 1, 1),
    )

    assert len(observations) == 1
    complete_at = observations[0].complete_at
    assert build_launch_profile_snapshot(observations, as_of=complete_at) == {}

    snapshot = build_launch_profile_snapshot(
        observations,
        as_of=complete_at + timedelta(days=1),
    )
    profile = select_launch_profile(
        item=item,
        snapshot=snapshot,
        scenario=stage_model_scenario("typical"),
        policy=policy,
        min_samples=1,
    )

    assert profile is not None
    assert profile.sample_count == 1
    assert profile.demand_qty_30d == Decimal("30")
    assert profile.min_qty == Decimal("30")
    assert profile.max_qty == Decimal("66")


def test_stage_recommendation_uses_launch_profile_for_new_item() -> None:
    policy = load_auto_order_policy(POLICY_PATH)
    lifecycle, evidence = historical_lifecycle_decision(
        item=_historical_item(),
        sales={},
        availability_dates=set(),
        purchases=[_historical_purchase()],
        receipts=[],
        as_of=date(2025, 6, 30),
        previous_status=None,
    )
    profile = LaunchProfile(
        scenario="typical",
        group_level="all_displays",
        group_key="||",
        sample_count=20,
        quantile=Decimal("0.50"),
        demand_qty_30d=Decimal("3"),
        min_qty=Decimal("3"),
        max_qty=Decimal("7"),
        confidence="medium",
    )

    recommended, target, decision, _tier, raw = stage_recommendation(
        lifecycle=lifecycle,
        rate=Decimal("0"),
        trend="flat_or_slowing",
        evidence=evidence,
        free_stock=Decimal("0"),
        incoming_qty=Decimal("0"),
        policy=policy,
        stage_scenario=stage_model_scenario("typical"),
        launch_profile=profile,
    )

    assert lifecycle.status.value == "new_item"
    assert target == Decimal("7")
    assert raw == Decimal("7")
    assert recommended == Decimal("7")
    assert decision == "manual_review"


def test_sales_start_min_max_does_not_use_twelve_minus_sales_cap() -> None:
    policy = load_auto_order_policy(POLICY_PATH)
    as_of = date(2025, 6, 30)
    sales = {as_of - timedelta(days=offset): Decimal("1") for offset in range(5)}
    lifecycle, evidence = historical_lifecycle_decision(
        item=_historical_item(),
        sales=sales,
        availability_dates=set(sales),
        purchases=[_historical_purchase()],
        receipts=[],
        as_of=as_of,
        previous_status="new_item",
    )

    recommended, target, decision, _tier, _raw = stage_recommendation(
        lifecycle=lifecycle,
        rate=Decimal("1"),
        trend="flat_or_slowing",
        evidence=evidence,
        free_stock=Decimal("0"),
        incoming_qty=Decimal("0"),
        policy=policy,
        stage_scenario=stage_model_scenario("typical"),
    )

    assert target == Decimal("66")
    assert recommended == Decimal("66")
    assert decision == "manual_review"


def test_stage_recommendation_removes_working_safety_stock() -> None:
    policy = load_auto_order_policy(POLICY_PATH)
    as_of = date(2026, 6, 30)
    old_item = _historical_item()
    old_item["source_record"] = {
        "created_at": "2024-01-01",
        "first_sale_at": "2025-06-01",
    }
    lifecycle, evidence = historical_lifecycle_decision(
        item=old_item,
        sales={},
        availability_dates=set(),
        purchases=[_historical_purchase()],
        receipts=[],
        as_of=as_of,
        previous_status="working",
    )
    recommended, target, decision, _tier, _raw = stage_recommendation(
        lifecycle=lifecycle,
        rate=Decimal("1"),
        trend="flat_or_slowing",
        evidence=evidence,
        free_stock=Decimal("0"),
        incoming_qty=Decimal("0"),
        policy=policy,
    )

    assert lifecycle.status.value == "working"
    assert target == Decimal("66")
    assert recommended == Decimal("66")
    assert decision == "order"


def test_service_scenario_protects_expensive_working_item() -> None:
    policy = load_auto_order_policy(POLICY_PATH)
    as_of = date(2026, 6, 30)
    old_item = _historical_item()
    old_item["expensive_profile"] = "expensive"
    old_item["source_record"] = {
        "created_at": "2024-01-01",
        "first_sale_at": "2025-06-01",
    }
    lifecycle, evidence = historical_lifecycle_decision(
        item=old_item,
        sales={},
        availability_dates=set(),
        purchases=[_historical_purchase()],
        receipts=[],
        as_of=as_of,
        previous_status="working",
    )

    recommended, target, decision, _tier, _raw = stage_recommendation(
        lifecycle=lifecycle,
        rate=Decimal("1"),
        trend="flat_or_slowing",
        evidence=evidence,
        free_stock=Decimal("0"),
        incoming_qty=Decimal("0"),
        policy=policy,
        item=old_item,
        stage_scenario=stage_model_scenario("service"),
    )

    assert target == Decimal("73")
    assert recommended == Decimal("73")
    assert decision == "order"


def test_item_is_not_in_historical_cohort_before_creation() -> None:
    item = _historical_item()

    assert not item_active_as_of(item, as_of=date(2024, 12, 31))
    assert item_active_as_of(item, as_of=date(2025, 1, 1))
