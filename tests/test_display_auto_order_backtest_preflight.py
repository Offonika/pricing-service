from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tasks.display_auto_order_backtest_preflight import (
    CarryingCostScenario,
    PreflightTables,
    build_demand_signal_queue_history,
    build_focused_scenario_definitions,
    build_kmp4_queue_history,
    calculate_economic_safety_stock,
    load_scenario_config,
    normalize_site_event_rows,
    select_lead_time_profile,
    validate_preflight_directory,
    write_preflight_artifacts,
)

CONFIG_PATH = Path("config/assortment/display-auto-order-backtest-scenarios.json")


def test_scenario_config_matches_approved_grid() -> None:
    config = load_scenario_config(CONFIG_PATH)

    assert config.kmp4_weights == (
        Decimal("0"),
        Decimal("0.5"),
        Decimal("1"),
    )
    assert config.kmp4_queue_days == 14
    assert config.site_queue_days == 14
    assert config.unordered_cart_daily_cap == Decimal("1")
    assert config.site_signals_enabled is True
    assert [row.name for row in config.site_signal_profiles] == [
        "off",
        "order_only",
        "cautious",
        "balanced",
        "service",
    ]
    assert [row.total_annual_rate for row in config.holding_cost_scenarios] == [
        Decimal("0.35"),
        Decimal("0.65"),
        Decimal("0.95"),
    ]
    assert config.grow_weekly_reduction_caps == (
        Decimal("0.1"),
        Decimal("0.2"),
        Decimal("0.3"),
    )
    assert config.grow_forecast_error_percentiles == (
        Decimal("0.75"),
        Decimal("0.9"),
        Decimal("0.95"),
    )
    assert config.grow_entry_protection_weeks == (2, 4, 6)
    assert [row.name for row in config.grow_service_floor_scenarios] == [
        "p75",
        "p90",
        "p90_budget",
    ]
    assert config.grow_service_floor_scenarios[-1].per_sku_cap_rub == Decimal("50000")
    assert config.grow_service_floor_scenarios[-1].stage_budget_rub == Decimal("8000000")
    assert [row.name for row in config.grow_acceleration_scenarios] == [
        "fast",
        "balanced",
        "strict",
    ]
    assert config.grow_acceleration_scenarios[1].recent_days == 14
    assert config.grow_acceleration_scenarios[1].baseline_days == 42
    assert config.grow_acceleration_scenarios[1].rate_multiplier == Decimal("1.5")
    assert config.grow_acceleration_scenarios[1].quantity_policy == (
        "protected_p90_no_economic_cap"
    )
    assert config.grow_acceleration_scenarios[1].per_sku_cap_rub == Decimal("0")
    assert config.grow_acceleration_scenarios[1].medium_pipeline_fraction == Decimal("0.75")


def test_focused_grow_scenario_design_is_balanced_and_contains_central_candidate() -> None:
    rows = build_focused_scenario_definitions(
        load_scenario_config(CONFIG_PATH),
        review_cadence_days=7,
    )
    protected = [
        row
        for row in rows
        if row["grow_weekly_reduction_cap"] != "0"
        and row["grow_service_floor_percentile"] == "0"
        and row["grow_acceleration_profile"] == "off"
    ]

    assert len(rows) == 26
    assert len(protected) == 18
    assert "typical_kmp0_5_sitebalanced_base" in {row["scenario_id"] for row in rows}
    assert "grow_cap20_p90_hold4_typical_kmp0_5_sitebalanced_base" in {
        row["scenario_id"] for row in rows
    }
    assert {
        cap: sum(row["grow_weekly_reduction_cap"] == cap for row in protected)
        for cap in ("0.1", "0.2", "0.3")
    } == {"0.1": 6, "0.2": 6, "0.3": 6}
    assert {
        percentile: sum(row["forecast_error_percentile"] == percentile for row in protected)
        for percentile in ("0.75", "0.9", "0.95")
    } == {"0.75": 6, "0.9": 6, "0.95": 6}
    assert {
        weeks: sum(row["grow_entry_protection_weeks"] == weeks for row in protected)
        for weeks in (2, 4, 6)
    } == {2: 6, 4: 6, 6: 6}
    service_floors = [row for row in rows if row["grow_service_floor_percentile"] != "0"]
    assert len(service_floors) == 3
    assert {row["grow_service_floor_percentile"] for row in service_floors} == {
        "0.75",
        "0.9",
    }
    assert {row["forecast_error_percentile"] for row in service_floors} == {"0.9"}
    budgeted = next(row for row in service_floors if "p90_budget" in row["scenario_id"])
    assert budgeted["grow_service_floor_sku_cap_rub"] == "50000"
    assert budgeted["grow_service_floor_stage_budget_rub"] == "8000000"
    acceleration = [row for row in rows if row["grow_acceleration_profile"] != "off"]
    assert len(acceleration) == 3
    assert {row["grow_acceleration_profile"] for row in acceleration} == {
        "fast",
        "balanced",
        "strict",
    }
    balanced = next(row for row in acceleration if row["grow_acceleration_profile"] == "balanced")
    assert balanced["grow_acceleration_recent_days"] == 14
    assert balanced["grow_acceleration_baseline_days"] == 42
    assert balanced["grow_acceleration_rate_multiplier"] == "1.5"
    assert balanced["grow_acceleration_quantity_policy"] == ("protected_p90_no_economic_cap")
    assert balanced["grow_acceleration_sku_cap_rub"] == "0"
    assert balanced["grow_acceleration_stage_budget_rub"] == "8000000"


def test_kmp4_queue_is_closed_by_later_sale_without_double_count() -> None:
    start = date(2026, 2, 1)
    result = build_kmp4_queue_history(
        codes=["SKU-1"],
        raw_demand_by_code={"SKU-1": {start: Decimal("3")}},
        sales_by_code={"SKU-1": {start + timedelta(days=3): Decimal("2")}},
        reserves_by_day={},
        date_from=start,
        date_to=start + timedelta(days=14),
        queue_days=14,
    )["SKU-1"]

    assert result[start].open_qty == Decimal("3")
    assert result[start + timedelta(days=3)].matched_qty == Decimal("2")
    assert result[start + timedelta(days=3)].open_qty == Decimal("1")
    assert result[start + timedelta(days=14)].expired_qty == Decimal("1")
    assert result[start + timedelta(days=14)].open_qty == Decimal("0")


def test_kmp4_queue_uses_reserve_increase_but_not_unchanged_balance() -> None:
    start = date(2026, 2, 1)
    reserves = {
        start: {"SKU-1": Decimal("0")},
        start + timedelta(days=1): {"SKU-1": Decimal("1")},
        start + timedelta(days=2): {"SKU-1": Decimal("1")},
    }
    result = build_kmp4_queue_history(
        codes=["SKU-1"],
        raw_demand_by_code={"SKU-1": {start: Decimal("2")}},
        sales_by_code={},
        reserves_by_day=reserves,
        date_from=start,
        date_to=start + timedelta(days=2),
        queue_days=14,
    )["SKU-1"]

    assert result[start + timedelta(days=1)].matched_qty == Decimal("1")
    assert result[start + timedelta(days=2)].matched_qty == Decimal("0")
    assert result[start + timedelta(days=2)].open_qty == Decimal("1")


def test_site_guid_mapping_cart_dedup_and_daily_cap() -> None:
    start = date(2026, 7, 1)
    rows = [
        {
            "event_date": start.isoformat(),
            "event_type": "site_unordered_cart",
            "product_xml_id": "2685293e-967c-11e1-bdb9-0025901e48ef",
            "quantity": "3",
            "session_key": "anon-1",
            "event_key": "event-1",
            "delay_flag": "N",
        },
        {
            "event_date": start.isoformat(),
            "event_type": "site_unordered_cart",
            "product_xml_id": "{2685293E-967C-11E1-BDB9-0025901E48EF}",
            "quantity": "2",
            "session_key": "anon-1",
            "event_key": "event-2",
            "delay_flag": "N",
        },
    ]

    result = normalize_site_event_rows(
        rows,
        product_refs={"SKU-1": "0xBDB90025901E48EF11E1967C2685293E"},
        cohort_codes=["SKU-1"],
        unordered_cart_daily_cap=Decimal("1"),
    )

    assert len(result.rows) == 1
    assert result.rows[0]["nomenclature_code"] == "SKU-1"
    assert result.rows[0]["raw_quantity"] == "5"
    assert result.rows[0]["quantity"] == "1"
    assert result.mapping_stats["cart_deduplicated_row_count"] == 1


def test_common_queue_prevents_sale_from_matching_kmp4_and_site_twice() -> None:
    start = date(2026, 2, 1)
    rows = [
        {
            "event_date": start.isoformat(),
            "event_type": "site_order",
            "nomenclature_code": "SKU-1",
            "quantity": "1",
            "event_key": "order-1",
            "mapping_status": "matched",
            "manual_review_only": 0,
        }
    ]

    result = build_demand_signal_queue_history(
        codes=["SKU-1"],
        kmp4_raw_by_code={"SKU-1": {start: Decimal("1")}},
        site_event_rows=rows,
        sales_by_code={"SKU-1": {start + timedelta(days=1): Decimal("1")}},
        reserves_by_day={},
        stock_by_day={},
        date_from=start,
        date_to=start + timedelta(days=1),
        queue_days=14,
    )["SKU-1"]

    assert result[start + timedelta(days=1)].kmp4_matched_qty == Decimal("1")
    assert result[start + timedelta(days=1)].site_order_matched_qty == Decimal("0")
    assert result[start + timedelta(days=1)].site_order_open_qty == Decimal("1")


def test_cart_is_quantitative_only_when_free_stock_is_not_positive() -> None:
    start = date(2026, 2, 1)
    row = {
        "event_date": start.isoformat(),
        "event_type": "site_unordered_cart",
        "nomenclature_code": "SKU-1",
        "quantity": "1",
        "event_key": "cart-1",
        "mapping_status": "matched",
        "manual_review_only": 0,
    }
    positive = build_demand_signal_queue_history(
        codes=["SKU-1"],
        kmp4_raw_by_code={},
        site_event_rows=[row],
        sales_by_code={},
        reserves_by_day={start: {"SKU-1": Decimal("0")}},
        stock_by_day={start: {"SKU-1": Decimal("1")}},
        date_from=start,
        date_to=start,
        queue_days=14,
    )["SKU-1"][start]
    stockout = build_demand_signal_queue_history(
        codes=["SKU-1"],
        kmp4_raw_by_code={},
        site_event_rows=[row],
        sales_by_code={},
        reserves_by_day={start: {"SKU-1": Decimal("0")}},
        stock_by_day={start: {"SKU-1": Decimal("0")}},
        date_from=start,
        date_to=start,
        queue_days=14,
    )["SKU-1"][start]

    assert positive.site_cart_raw_qty == Decimal("0")
    assert positive.site_cart_stock_blocked_qty == Decimal("1")
    assert stockout.site_cart_raw_qty == Decimal("1")


def test_negative_reserve_never_increases_stock_and_backlog_cancel_is_not_sale() -> None:
    start = date(2026, 2, 1)
    result = build_demand_signal_queue_history(
        codes=["SKU-1"],
        kmp4_raw_by_code={},
        site_event_rows=[],
        sales_by_code={},
        reserves_by_day={
            start: {"SKU-1": Decimal("-1")},
            start + timedelta(days=1): {"SKU-1": Decimal("3")},
            start + timedelta(days=2): {"SKU-1": Decimal("0")},
        },
        stock_by_day={
            start: {"SKU-1": Decimal("0")},
            start + timedelta(days=1): {"SKU-1": Decimal("1")},
            start + timedelta(days=2): {"SKU-1": Decimal("1")},
        },
        date_from=start,
        date_to=start + timedelta(days=2),
        queue_days=14,
    )["SKU-1"]

    assert result[start].raw_reserve_qty == Decimal("-1")
    assert result[start].effective_reserve_qty == Decimal("0")
    assert result[start + timedelta(days=1)].reserve_backlog_qty == Decimal("2")
    assert result[start + timedelta(days=2)].reserve_backlog_cancelled_qty == Decimal("2")
    assert result[start + timedelta(days=2)].reserve_backlog_matched_qty == Decimal("0")


def test_future_site_cancellation_does_not_change_past_queue() -> None:
    start = date(2026, 2, 1)
    result = build_demand_signal_queue_history(
        codes=["SKU-1"],
        kmp4_raw_by_code={},
        site_event_rows=[
            {
                "event_date": start.isoformat(),
                "event_type": "site_order",
                "nomenclature_code": "SKU-1",
                "quantity": "2",
                "event_key": "order-1",
                "cancelled_at": (start + timedelta(days=2)).isoformat(),
                "mapping_status": "matched",
                "manual_review_only": 0,
            }
        ],
        sales_by_code={},
        reserves_by_day={},
        stock_by_day={},
        date_from=start,
        date_to=start + timedelta(days=2),
        queue_days=14,
    )["SKU-1"]

    assert result[start].site_order_open_qty == Decimal("2")
    assert result[start + timedelta(days=1)].site_order_open_qty == Decimal("2")
    assert result[start + timedelta(days=2)].site_order_cancelled_qty == Decimal("2")
    assert result[start + timedelta(days=2)].site_order_open_qty == Decimal("0")


def test_lead_time_profile_excludes_receipts_not_known_on_decision_date() -> None:
    config = load_scenario_config(CONFIG_PATH)
    rows = [
        {
            "nomenclature_code": "SKU-1",
            "display_group_key": "group",
            "supplier_ref": "SUP-1",
            "supplier_name": "Supplier",
            "warehouse_receipt_at": "2026-01-01",
            "total_arrival_days": "20",
        },
        {
            "nomenclature_code": "SKU-1",
            "display_group_key": "group",
            "supplier_ref": "SUP-1",
            "supplier_name": "Supplier",
            "warehouse_receipt_at": "2026-01-10",
            "total_arrival_days": "40",
        },
        {
            "nomenclature_code": "SKU-1",
            "display_group_key": "group",
            "supplier_ref": "SUP-1",
            "supplier_name": "Supplier",
            "warehouse_receipt_at": "2026-03-01",
            "total_arrival_days": "100",
        },
    ]

    profile = select_lead_time_profile(
        rows,
        code="SKU-1",
        group_key="group",
        supplier_ref="SUP-1",
        supplier_name="Supplier",
        as_of=date(2026, 2, 1),
        config=config,
    )

    assert profile.source_level == "sku_supplier"
    assert profile.sample_count == 2
    assert profile.p50_days == 20
    assert profile.p75_days == 40
    assert profile.confidence == "medium"


def test_economic_safety_stock_stops_at_first_unprofitable_unit() -> None:
    scenario = CarryingCostScenario(
        name="base",
        capital_annual_rate=Decimal("0.30"),
        storage_annual_rate=Decimal("0.10"),
        obsolescence_annual_rate=Decimal("0.25"),
    )

    result = calculate_economic_safety_stock(
        base_max_qty=Decimal("2"),
        demand_samples=[Decimal(value) for value in (2, 3, 3, 4, 4, 4, 5, 5)],
        gross_margin_per_unit_rub=Decimal("100"),
        inventory_cost_per_unit_rub=Decimal("1000"),
        holding_days=30,
        cost_scenario=scenario,
        max_units=10,
        min_samples=8,
    )

    assert result.units == Decimal("2")
    assert result.expected_saved_margin_rub > result.carrying_cost_rub
    assert result.marginal_saved_margin_rub <= result.marginal_carrying_cost_rub


def test_economic_safety_stock_applies_hurdle_to_each_incremental_unit() -> None:
    scenario = CarryingCostScenario(
        name="base",
        capital_annual_rate=Decimal("0.30"),
        storage_annual_rate=Decimal("0.10"),
        obsolescence_annual_rate=Decimal("0.25"),
    )

    result = calculate_economic_safety_stock(
        base_max_qty=Decimal("2"),
        demand_samples=[Decimal(value) for value in (2, 3, 3, 4, 4, 4, 5, 5)],
        gross_margin_per_unit_rub=Decimal("100"),
        inventory_cost_per_unit_rub=Decimal("1000"),
        holding_days=30,
        cost_scenario=scenario,
        max_units=10,
        min_samples=8,
        hurdle_multiplier=Decimal("1.5"),
    )

    assert result.units == Decimal("1")
    assert result.marginal_carrying_cost_rub > Decimal("80")


def test_preflight_manifest_checks_status_and_hashes(tmp_path: Path) -> None:
    row = {"decision_date": "2026-02-01", "nomenclature_code": "SKU-1"}
    tables = PreflightTables(
        decision_inputs=[row],
        scenario_decisions=[{"scenario_id": "legacy", **row}],
        lifecycle_daily=[{"business_date": "2026-02-01", **row}],
        daily_facts=[{"business_date": "2026-02-01", **row}],
        historical_sales=[
            {
                "business_date": "2026-01-31",
                "nomenclature_code": "SKU-1",
                "observed_sales_qty": "1",
            }
        ],
        initial_pipeline=[{"nomenclature_code": "SKU-1", "quantity": "1"}],
        source_quality=[{"check": "keys", "status": "pass"}],
        reconciliations=[{"source": "reserve", "status": "pass"}],
        site_events=[],
        status="PASS",
        scope_audit={
            "scope_policy_version": "display_scope_policy.v1",
            "source_item_count": 2,
            "included_item_count": 1,
            "excluded_item_count": 1,
            "excluded_row_count": 1,
            "excluded_reason_counts": {"excluded_display_name_bitok": 1},
            "exclusions": [],
        },
    )
    site_csv = tmp_path / "source-site-events.csv"
    site_csv.write_text(
        "event_date,event_type,product_xml_id,quantity,order_number,cancelled_at,session_key,event_key,delay_flag\n",
        encoding="utf-8",
    )
    write_preflight_artifacts(
        tmp_path,
        tables=tables,
        date_from=date(2026, 2, 1),
        date_to=date(2026, 7, 31),
        history_start=date(2025, 1, 1),
        config_path=CONFIG_PATH,
        cohort_run_id=1,
        site_events_csv=site_csv,
        site_mapping_stats={"mapped_row_count": 0},
    )

    manifest = validate_preflight_directory(tmp_path)
    assert manifest["preflight_status"] == "PASS"
    assert manifest["schema"] == "display_auto_order_backtest_preflight.v2"
    assert manifest["row_counts"]["historical_sales"] == 1
    assert manifest["scope_policy"]["excluded_item_count"] == 1

    (tmp_path / "decision-inputs.csv").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="checksum mismatch"):
        validate_preflight_directory(tmp_path)
