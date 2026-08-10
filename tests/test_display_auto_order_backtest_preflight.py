from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tasks.display_auto_order_backtest_preflight import (
    CarryingCostScenario,
    PreflightTables,
    build_kmp4_queue_history,
    calculate_economic_safety_stock,
    load_scenario_config,
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
    assert config.site_signals_enabled is False
    assert [row.total_annual_rate for row in config.holding_cost_scenarios] == [
        Decimal("0.35"),
        Decimal("0.65"),
        Decimal("0.95"),
    ]


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


def test_preflight_manifest_checks_status_and_hashes(tmp_path: Path) -> None:
    row = {"decision_date": "2026-02-01", "nomenclature_code": "SKU-1"}
    tables = PreflightTables(
        decision_inputs=[row],
        scenario_decisions=[{"scenario_id": "legacy", **row}],
        lifecycle_daily=[{"business_date": "2026-02-01", **row}],
        daily_facts=[{"business_date": "2026-02-01", **row}],
        initial_pipeline=[{"nomenclature_code": "SKU-1", "quantity": "1"}],
        source_quality=[{"check": "keys", "status": "pass"}],
        reconciliations=[{"source": "reserve", "status": "pass"}],
        status="PASS",
    )
    write_preflight_artifacts(
        tmp_path,
        tables=tables,
        date_from=date(2026, 2, 1),
        date_to=date(2026, 7, 31),
        history_start=date(2025, 1, 1),
        config_path=CONFIG_PATH,
        cohort_run_id=1,
    )

    manifest = validate_preflight_directory(tmp_path)
    assert manifest["preflight_status"] == "PASS"

    (tmp_path / "decision-inputs.csv").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="checksum mismatch"):
        validate_preflight_directory(tmp_path)
