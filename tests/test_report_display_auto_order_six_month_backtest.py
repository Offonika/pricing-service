from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from tasks.build_display_auto_order_dry_run import load_auto_order_policy
from tasks.report_display_auto_order_six_month_backtest import (
    PipelineLot,
    PurchaseLine,
    ReceiptLine,
    actual_purchase_summary,
    actual_stock_summary,
    forecast_rate,
    initial_pipeline,
    run_simulation,
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
