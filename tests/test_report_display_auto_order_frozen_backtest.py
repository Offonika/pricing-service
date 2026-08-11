from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from tasks.build_display_auto_order_dry_run import AutoOrderPolicy
from tasks.display_auto_order_backtest_preflight import (
    CarryingCostScenario,
    load_scenario_config,
)
from tasks.report_display_auto_order_frozen_backtest import (
    FrozenScenario,
    ServiceFloorCandidate,
    _free_initial_pipeline,
    allocate_service_floor_budget,
    apply_grow_target_protection,
    apply_service_floor_sku_cap,
    empirical_underforecast_percentile,
    historical_forecast_error_samples,
    simulate_scenario,
)


def test_service_floor_sku_cap_uses_whole_affordable_units() -> None:
    assert apply_service_floor_sku_cap(
        Decimal("5"),
        unit_cost_rub=Decimal("30"),
        per_sku_cap_rub=Decimal("100"),
    ) == Decimal("3")


def test_service_floor_stage_budget_prioritizes_marginal_saved_margin() -> None:
    allocated = allocate_service_floor_budget(
        [
            ServiceFloorCandidate(
                code="HIGH",
                requested_units=Decimal("2"),
                unit_cost_rub=Decimal("50"),
                gross_margin_per_unit_rub=Decimal("100"),
                error_samples=(Decimal("2"),) * 8,
            ),
            ServiceFloorCandidate(
                code="LOW",
                requested_units=Decimal("2"),
                unit_cost_rub=Decimal("50"),
                gross_margin_per_unit_rub=Decimal("10"),
                error_samples=(Decimal("2"),) * 8,
            ),
        ],
        stage_budget_rub=Decimal("100"),
    )

    assert allocated == {"HIGH": Decimal("2"), "LOW": Decimal("0")}


def test_initial_pipeline_excludes_customer_placed_quantity() -> None:
    start = date(2026, 2, 1)
    result = _free_initial_pipeline(
        [
            {
                "nomenclature_code": "SKU-1",
                "arrival_at": start.isoformat(),
                "quantity": "3",
            },
            {
                "nomenclature_code": "SKU-1",
                "arrival_at": (start + timedelta(days=1)).isoformat(),
                "quantity": "4",
            },
        ],
        placed_by_code={"SKU-1": Decimal("5")},
        date_from=start,
    )

    assert result == {"SKU-1": [(start + timedelta(days=1), Decimal("2"))]}


def test_historical_forecast_errors_use_only_completed_past_windows() -> None:
    first = date(2026, 1, 1)
    as_of = date(2026, 1, 5)
    rows = [
        {
            "decision_date": first.isoformat(),
            "scheduled_review": "1",
            "lead_time_p50_days": "2",
            "forecast_rate_sales": "1",
        },
        {
            "decision_date": (as_of - timedelta(days=1)).isoformat(),
            "scheduled_review": "1",
            "lead_time_p50_days": "2",
            "forecast_rate_sales": "0",
        },
    ]
    sales = {
        first + timedelta(days=1): Decimal("2"),
        first + timedelta(days=2): Decimal("2"),
        first + timedelta(days=3): Decimal("1"),
        as_of + timedelta(days=10): Decimal("1000"),
    }

    result = historical_forecast_error_samples(
        rows,
        sales,
        as_of=as_of,
        order_cadence_days=1,
        lookback_days=365,
    )

    assert result == [Decimal("2")]


def test_empirical_underforecast_percentile_uses_nearest_rank() -> None:
    samples = [Decimal(value) for value in range(1, 9)]

    assert empirical_underforecast_percentile(
        samples,
        percentile=Decimal("0.75"),
        min_samples=8,
    ) == Decimal("6")
    assert empirical_underforecast_percentile(
        samples,
        percentile=Decimal("0.90"),
        min_samples=8,
    ) == Decimal("8")


def test_grow_target_holds_after_entry_then_reduces_only_by_weekly_cap() -> None:
    start = date(2026, 2, 1)
    minimum, maximum, state, reason = apply_grow_target_protection(
        raw_min_qty=Decimal("50"),
        raw_max_qty=Decimal("100"),
        as_of=start,
        scheduled_review=True,
        entered_today=True,
        weekly_reduction_cap=Decimal("0.2"),
        entry_protection_weeks=2,
        state=None,
    )
    assert (minimum, maximum, reason) == (Decimal("50"), Decimal("100"), "entry_hold")

    minimum, maximum, state, reason = apply_grow_target_protection(
        raw_min_qty=Decimal("10"),
        raw_max_qty=Decimal("20"),
        as_of=start + timedelta(days=7),
        scheduled_review=True,
        entered_today=False,
        weekly_reduction_cap=Decimal("0.2"),
        entry_protection_weeks=2,
        state=state,
    )
    assert (minimum, maximum, reason) == (Decimal("50"), Decimal("100"), "entry_hold")

    minimum, maximum, state, reason = apply_grow_target_protection(
        raw_min_qty=Decimal("10"),
        raw_max_qty=Decimal("20"),
        as_of=start + timedelta(days=14),
        scheduled_review=True,
        entered_today=False,
        weekly_reduction_cap=Decimal("0.2"),
        entry_protection_weeks=2,
        state=state,
    )
    assert (minimum, maximum, reason) == (
        Decimal("40"),
        Decimal("80"),
        "weekly_reduction_cap",
    )

    minimum, maximum, _, reason = apply_grow_target_protection(
        raw_min_qty=Decimal("5"),
        raw_max_qty=Decimal("10"),
        as_of=start + timedelta(days=15),
        scheduled_review=False,
        entered_today=False,
        weekly_reduction_cap=Decimal("0.2"),
        entry_protection_weeks=2,
        state=state,
    )
    assert (minimum, maximum, reason) == (
        Decimal("40"),
        Decimal("80"),
        "between_reviews_floor",
    )


def test_frozen_weekly_min_max_order_arrives_and_serves_demand() -> None:
    start = date(2026, 2, 1)
    second = start + timedelta(days=1)
    facts = {
        start: [
            {
                "nomenclature_code": "SKU-1",
                "physical_stock_qty": "0",
                "observed_sales_qty": "0",
                "placed_incoming_qty": "0",
                "kmp4_expired_qty": "0",
            }
        ],
        second: [
            {
                "nomenclature_code": "SKU-1",
                "physical_stock_qty": "0",
                "observed_sales_qty": "1",
                "placed_incoming_qty": "0",
                "kmp4_expired_qty": "0",
            }
        ],
    }
    decisions = {
        start: [
            {
                "nomenclature_code": "SKU-1",
                "scheduled_review": "1",
                "status": "working",
                "forecast_rate_sales": "1",
                "kmp4_open_qty": "0",
                "lead_time_p50_days": "1",
                "lead_time_p75_days": "1",
                "lead_time_confidence": "high",
                "inventory_cost_per_unit_rub": "100",
                "gross_margin_per_unit_rub": "10",
                "reserve_qty": "0",
            }
        ]
    }
    scenario = FrozenScenario(
        scenario_id="typical_kmp0_base",
        stage_profile="typical",
        kmp4_weight=Decimal("0"),
        cost=CarryingCostScenario(
            name="base",
            capital_annual_rate=Decimal("0.3"),
            storage_annual_rate=Decimal("0.1"),
            obsolescence_annual_rate=Decimal("0.25"),
        ),
    )

    result = simulate_scenario(
        scenario=scenario,
        fact_rows_by_date=facts,
        decision_rows_by_date=decisions,
        initial_pipeline_rows=[],
        sales_by_code={"SKU-1": {second: Decimal("1")}},
        policy=AutoOrderPolicy(order_cadence_days=1),
        config=load_scenario_config(
            Path("config/assortment/display-auto-order-backtest-scenarios.json")
        ),
        date_from=start,
        date_to=second,
        keep_detail=False,
    )

    assert result.model["SKU-1"].order_qty == Decimal("3")
    assert result.model["SKU-1"].served_observed_qty == Decimal("1")
    assert result.model["SKU-1"].lost_observed_qty == Decimal("0")


def test_event_only_review_is_accepted_for_new_model_but_not_legacy() -> None:
    start = date(2026, 2, 1)
    second = start + timedelta(days=1)
    facts = {
        start: [
            {
                "nomenclature_code": "SKU-1",
                "physical_stock_qty": "0",
                "observed_sales_qty": "0",
                "placed_incoming_qty": "0",
                "kmp4_expired_qty": "0",
            }
        ],
        second: [
            {
                "nomenclature_code": "SKU-1",
                "physical_stock_qty": "0",
                "observed_sales_qty": "1",
                "placed_incoming_qty": "0",
                "kmp4_expired_qty": "0",
            }
        ],
    }
    decisions = {
        start: [
            {
                "nomenclature_code": "SKU-1",
                "scheduled_review": "0",
                "event_review": "1",
                "status": "working",
                "forecast_rate_sales": "1",
                "kmp4_open_qty": "0",
                "lead_time_p50_days": "1",
                "lead_time_p75_days": "1",
                "lead_time_confidence": "high",
                "inventory_cost_per_unit_rub": "100",
                "gross_margin_per_unit_rub": "10",
                "reserve_qty": "0",
            }
        ]
    }
    cost = CarryingCostScenario(
        name="base",
        capital_annual_rate=Decimal("0.3"),
        storage_annual_rate=Decimal("0.1"),
        obsolescence_annual_rate=Decimal("0.25"),
    )
    common = {
        "fact_rows_by_date": facts,
        "decision_rows_by_date": decisions,
        "initial_pipeline_rows": [],
        "sales_by_code": {"SKU-1": {second: Decimal("1")}},
        "policy": AutoOrderPolicy(order_cadence_days=1),
        "config": load_scenario_config(
            Path("config/assortment/display-auto-order-backtest-scenarios.json")
        ),
        "date_from": start,
        "date_to": second,
        "keep_detail": False,
    }

    new_model = simulate_scenario(
        scenario=FrozenScenario(
            scenario_id="typical_kmp0_base",
            stage_profile="typical",
            kmp4_weight=Decimal("0"),
            cost=cost,
        ),
        **common,
    )
    legacy = simulate_scenario(
        scenario=FrozenScenario(
            scenario_id="legacy",
            stage_profile="legacy",
            kmp4_weight=Decimal("0"),
            cost=cost,
            legacy=True,
        ),
        **common,
    )

    assert new_model.model["SKU-1"].order_qty == Decimal("3")
    assert new_model.model["SKU-1"].manual_order_lines == 2
    assert new_model.model["SKU-1"].manual_review_created == 1
    assert new_model.model["SKU-1"].manual_review_updated == 1
    assert new_model.model["SKU-1"].served_observed_qty == Decimal("1")
    assert legacy.model["SKU-1"].order_qty == Decimal("0")
    assert legacy.model["SKU-1"].lost_observed_qty == Decimal("1")


def test_sku_launched_inside_period_receives_its_exogenous_opening_supply() -> None:
    start = date(2026, 2, 1)
    launch = start + timedelta(days=1)
    scenario = FrozenScenario(
        scenario_id="typical_kmp0_base",
        stage_profile="typical",
        kmp4_weight=Decimal("0"),
        cost=CarryingCostScenario(
            name="base",
            capital_annual_rate=Decimal("0.3"),
            storage_annual_rate=Decimal("0.1"),
            obsolescence_annual_rate=Decimal("0.25"),
        ),
    )

    result = simulate_scenario(
        scenario=scenario,
        fact_rows_by_date={
            start: [
                {
                    "nomenclature_code": "EXISTING",
                    "status": "working",
                    "physical_stock_qty": "0",
                    "observed_sales_qty": "0",
                }
            ],
            launch: [
                {
                    "nomenclature_code": "NEW-SKU",
                    "status": "sales_start",
                    "physical_stock_qty": "2",
                    "observed_sales_qty": "1",
                }
            ],
        },
        decision_rows_by_date={},
        initial_pipeline_rows=[],
        sales_by_code={"NEW-SKU": {launch: Decimal("1")}},
        policy=AutoOrderPolicy(order_cadence_days=1),
        config=load_scenario_config(
            Path("config/assortment/display-auto-order-backtest-scenarios.json")
        ),
        date_from=start,
        date_to=launch,
        keep_detail=False,
    )

    assert result.model["NEW-SKU"].exogenous_launch_seed_qty == Decimal("3")
    assert result.model["NEW-SKU"].served_observed_qty == Decimal("1")
    assert result.model["NEW-SKU"].lost_observed_qty == Decimal("0")
    assert result.model["NEW-SKU"].ending_inventory_qty == Decimal("2")


def test_existing_sale_sku_is_not_reseeded_from_future_actual_stock() -> None:
    start = date(2026, 2, 1)
    second = start + timedelta(days=1)
    scenario = FrozenScenario(
        scenario_id="typical_kmp0_base",
        stage_profile="typical",
        kmp4_weight=Decimal("0"),
        cost=CarryingCostScenario(
            name="base",
            capital_annual_rate=Decimal("0.3"),
            storage_annual_rate=Decimal("0.1"),
            obsolescence_annual_rate=Decimal("0.25"),
        ),
    )

    result = simulate_scenario(
        scenario=scenario,
        fact_rows_by_date={
            start: [
                {
                    "nomenclature_code": "SKU-1",
                    "status": "sale",
                    "physical_stock_qty": "0",
                    "observed_sales_qty": "0",
                }
            ],
            second: [
                {
                    "nomenclature_code": "SKU-1",
                    "status": "sale",
                    "physical_stock_qty": "5",
                    "observed_sales_qty": "1",
                }
            ],
        },
        decision_rows_by_date={},
        initial_pipeline_rows=[],
        sales_by_code={"SKU-1": {second: Decimal("1")}},
        policy=AutoOrderPolicy(order_cadence_days=1),
        config=load_scenario_config(
            Path("config/assortment/display-auto-order-backtest-scenarios.json")
        ),
        date_from=start,
        date_to=second,
        keep_detail=False,
    )

    assert result.model["SKU-1"].exogenous_launch_seed_qty == Decimal("0")
    assert result.model["SKU-1"].served_observed_qty == Decimal("0")
    assert result.model["SKU-1"].lost_observed_qty == Decimal("1")


def test_new_item_profile_waits_for_first_real_stock() -> None:
    start = date(2026, 2, 1)
    scenario = FrozenScenario(
        scenario_id="typical_kmp0_base",
        stage_profile="typical",
        kmp4_weight=Decimal("0"),
        cost=CarryingCostScenario(
            name="base",
            capital_annual_rate=Decimal("0.3"),
            storage_annual_rate=Decimal("0.1"),
            obsolescence_annual_rate=Decimal("0.25"),
        ),
    )

    result = simulate_scenario(
        scenario=scenario,
        fact_rows_by_date={
            start: [
                {
                    "nomenclature_code": "SKU-1",
                    "status": "new_item",
                    "physical_stock_qty": "0",
                    "observed_sales_qty": "0",
                }
            ]
        },
        decision_rows_by_date={
            start: [
                {
                    "nomenclature_code": "SKU-1",
                    "scheduled_review": "1",
                    "status": "new_item",
                    "forecast_rate_sales": "0",
                    "kmp4_open_qty": "0",
                    "lead_time_p50_days": "10",
                    "lead_time_p75_days": "10",
                    "lead_time_confidence": "high",
                    "inventory_cost_per_unit_rub": "100",
                    "gross_margin_per_unit_rub": "10",
                    "reserve_qty": "0",
                    "launch_typical_demand_qty_30d": "10",
                    "launch_typical_min_qty": "5",
                    "launch_typical_max_qty": "12",
                }
            ]
        },
        initial_pipeline_rows=[],
        sales_by_code={},
        policy=AutoOrderPolicy(order_cadence_days=7),
        config=load_scenario_config(
            Path("config/assortment/display-auto-order-backtest-scenarios.json")
        ),
        date_from=start,
        date_to=start,
        keep_detail=False,
    )

    assert result.model["SKU-1"].order_qty == Decimal("0")


def test_kmp4_queue_is_added_once_instead_of_extrapolated_as_daily_sales() -> None:
    start = date(2026, 2, 1)
    scenario = FrozenScenario(
        scenario_id="typical_kmp0_5_base",
        stage_profile="typical",
        kmp4_weight=Decimal("0.5"),
        cost=CarryingCostScenario(
            name="base",
            capital_annual_rate=Decimal("0.3"),
            storage_annual_rate=Decimal("0.1"),
            obsolescence_annual_rate=Decimal("0.25"),
        ),
    )

    result = simulate_scenario(
        scenario=scenario,
        fact_rows_by_date={
            start: [
                {
                    "nomenclature_code": "SKU-1",
                    "status": "working",
                    "physical_stock_qty": "0",
                    "observed_sales_qty": "0",
                    "kmp4_expired_qty": "0",
                }
            ]
        },
        decision_rows_by_date={
            start: [
                {
                    "nomenclature_code": "SKU-1",
                    "scheduled_review": "1",
                    "status": "working",
                    "forecast_rate_sales": "0",
                    "kmp4_open_qty": "14",
                    "lead_time_p50_days": "50",
                    "lead_time_p75_days": "50",
                    "lead_time_confidence": "high",
                    "inventory_cost_per_unit_rub": "100",
                    "gross_margin_per_unit_rub": "10",
                    "reserve_qty": "0",
                }
            ]
        },
        initial_pipeline_rows=[],
        sales_by_code={},
        policy=AutoOrderPolicy(order_cadence_days=7),
        config=load_scenario_config(
            Path("config/assortment/display-auto-order-backtest-scenarios.json")
        ),
        date_from=start,
        date_to=start,
        keep_detail=False,
    )

    assert result.model["SKU-1"].order_qty == Decimal("7")


def test_p75_changes_coverage_but_not_simulated_arrival_date() -> None:
    start = date(2026, 2, 1)
    second = start + timedelta(days=1)
    scenario = FrozenScenario(
        scenario_id="typical_kmp0_base",
        stage_profile="typical",
        kmp4_weight=Decimal("0"),
        cost=CarryingCostScenario(
            name="base",
            capital_annual_rate=Decimal("0.3"),
            storage_annual_rate=Decimal("0.1"),
            obsolescence_annual_rate=Decimal("0.25"),
        ),
    )

    result = simulate_scenario(
        scenario=scenario,
        fact_rows_by_date={
            start: [
                {
                    "nomenclature_code": "SKU-1",
                    "status": "working",
                    "physical_stock_qty": "0",
                    "observed_sales_qty": "0",
                    "kmp4_expired_qty": "0",
                }
            ],
            second: [
                {
                    "nomenclature_code": "SKU-1",
                    "status": "working",
                    "physical_stock_qty": "0",
                    "observed_sales_qty": "1",
                    "kmp4_expired_qty": "0",
                }
            ],
        },
        decision_rows_by_date={
            start: [
                {
                    "nomenclature_code": "SKU-1",
                    "scheduled_review": "1",
                    "status": "working",
                    "forecast_rate_sales": "0",
                    "kmp4_open_qty": "0",
                    "lead_time_p50_days": "1",
                    "lead_time_p75_days": "5",
                    "lead_time_confidence": "high",
                    "inventory_cost_per_unit_rub": "1",
                    "gross_margin_per_unit_rub": "100",
                    "reserve_qty": "0",
                }
            ]
        },
        initial_pipeline_rows=[],
        sales_by_code={"SKU-1": {second: Decimal("1")}},
        policy=AutoOrderPolicy(order_cadence_days=7),
        config=load_scenario_config(
            Path("config/assortment/display-auto-order-backtest-scenarios.json")
        ),
        date_from=start,
        date_to=second,
        keep_detail=True,
        demand_sample_cache={
            ("SKU-1", start, 8): [Decimal("10")] * 8,
            ("SKU-1", start, 12): [Decimal("10")] * 8,
        },
    )

    assert result.model["SKU-1"].served_observed_qty == Decimal("1")
    assert result.decision_rows[0]["selected_lead_time_days"] == 5
    assert result.decision_rows[0]["simulated_arrival_lead_time_days"] == 1


def test_grow_safety_stock_uses_selected_error_percentile_with_economic_cap() -> None:
    start = date(2026, 2, 1)
    scenario = FrozenScenario(
        scenario_id="grow_cap20_p75_hold2",
        stage_profile="typical",
        kmp4_weight=Decimal("0.5"),
        site_profile="balanced",
        site_order_weight=Decimal("1"),
        site_unordered_cart_weight=Decimal("0.25"),
        grow_weekly_reduction_cap=Decimal("0.2"),
        forecast_error_percentile=Decimal("0.75"),
        grow_entry_protection_weeks=2,
        cost=CarryingCostScenario(
            name="base",
            capital_annual_rate=Decimal("0.3"),
            storage_annual_rate=Decimal("0.1"),
            obsolescence_annual_rate=Decimal("0.25"),
        ),
    )

    result = simulate_scenario(
        scenario=scenario,
        fact_rows_by_date={
            start: [
                {
                    "nomenclature_code": "SKU-1",
                    "status": "sale",
                    "previous_status": "working",
                    "physical_stock_qty": "0",
                    "observed_sales_qty": "0",
                }
            ]
        },
        decision_rows_by_date={
            start: [
                {
                    "nomenclature_code": "SKU-1",
                    "scheduled_review": "1",
                    "status": "sale",
                    "forecast_rate_sales": "0",
                    "lead_time_p50_days": "1",
                    "lead_time_p75_days": "1",
                    "lead_time_confidence": "high",
                    "inventory_cost_per_unit_rub": "1",
                    "gross_margin_per_unit_rub": "100",
                }
            ]
        },
        initial_pipeline_rows=[],
        sales_by_code={},
        policy=AutoOrderPolicy(order_cadence_days=1),
        config=load_scenario_config(
            Path("config/assortment/display-auto-order-backtest-scenarios.json")
        ),
        date_from=start,
        date_to=start,
        keep_detail=True,
        demand_sample_cache={("SKU-1", start, 2): [Decimal(value) for value in range(1, 9)]},
    )

    assert result.model["SKU-1"].order_qty == Decimal("6")
    assert result.decision_rows[0]["forecast_error_percentile_qty"] == "6"
    assert Decimal(result.decision_rows[0]["economic_safety_cap_qty"]) >= Decimal("6")
    assert result.decision_rows[0]["economic_safety_stock_qty"] == "6"


def test_grow_service_floor_is_not_cut_by_sku_economic_filter() -> None:
    start = date(2026, 2, 1)
    scenario = FrozenScenario(
        scenario_id="grow_servicefloor_p75",
        stage_profile="typical",
        kmp4_weight=Decimal("0.5"),
        grow_weekly_reduction_cap=Decimal("0.2"),
        forecast_error_percentile=Decimal("0.75"),
        grow_entry_protection_weeks=2,
        grow_service_floor_percentile=Decimal("0.75"),
        cost=CarryingCostScenario(
            name="base",
            capital_annual_rate=Decimal("0.3"),
            storage_annual_rate=Decimal("0.1"),
            obsolescence_annual_rate=Decimal("0.25"),
        ),
    )

    result = simulate_scenario(
        scenario=scenario,
        fact_rows_by_date={
            start: [
                {
                    "nomenclature_code": "SKU-1",
                    "status": "sale",
                    "previous_status": "working",
                    "physical_stock_qty": "0",
                    "observed_sales_qty": "0",
                }
            ]
        },
        decision_rows_by_date={
            start: [
                {
                    "nomenclature_code": "SKU-1",
                    "scheduled_review": "1",
                    "status": "sale",
                    "forecast_rate_sales": "0",
                    "lead_time_p50_days": "1",
                    "lead_time_p75_days": "1",
                    "lead_time_confidence": "high",
                    "inventory_cost_per_unit_rub": "1000",
                    "gross_margin_per_unit_rub": "1",
                }
            ]
        },
        initial_pipeline_rows=[],
        sales_by_code={},
        policy=AutoOrderPolicy(order_cadence_days=1),
        config=load_scenario_config(
            Path("config/assortment/display-auto-order-backtest-scenarios.json")
        ),
        date_from=start,
        date_to=start,
        keep_detail=True,
        demand_sample_cache={("SKU-1", start, 2): [Decimal(value) for value in range(1, 9)]},
    )

    decision = result.decision_rows[0]
    assert decision["economic_safety_cap_qty"] == "0"
    assert decision["service_floor_requested_qty"] == "6"
    assert decision["service_floor_allocated_qty"] == "6"
    assert result.model["SKU-1"].order_qty == Decimal("6")


def test_daily_stockout_guard_uses_latest_weekly_min_max() -> None:
    start = date(2026, 2, 1)
    second = start + timedelta(days=1)
    scenario = FrozenScenario(
        scenario_id="typical_kmp0_base",
        stage_profile="typical",
        kmp4_weight=Decimal("0"),
        cost=CarryingCostScenario(
            name="base",
            capital_annual_rate=Decimal("0.3"),
            storage_annual_rate=Decimal("0.1"),
            obsolescence_annual_rate=Decimal("0.25"),
        ),
    )

    result = simulate_scenario(
        scenario=scenario,
        fact_rows_by_date={
            start: [
                {
                    "nomenclature_code": "SKU-1",
                    "status": "working",
                    "physical_stock_qty": "3",
                    "observed_sales_qty": "0",
                    "reserve_qty": "0",
                }
            ],
            second: [
                {
                    "nomenclature_code": "SKU-1",
                    "status": "working",
                    "physical_stock_qty": "2",
                    "observed_sales_qty": "1",
                    "reserve_qty": "0",
                }
            ],
        },
        decision_rows_by_date={
            start: [
                {
                    "nomenclature_code": "SKU-1",
                    "scheduled_review": "1",
                    "status": "working",
                    "forecast_rate_sales": "1",
                    "kmp4_open_qty": "0",
                    "lead_time_p50_days": "2",
                    "lead_time_p75_days": "2",
                    "lead_time_confidence": "high",
                    "inventory_cost_per_unit_rub": "100",
                    "gross_margin_per_unit_rub": "10",
                    "reserve_qty": "0",
                }
            ]
        },
        initial_pipeline_rows=[],
        sales_by_code={"SKU-1": {second: Decimal("1")}},
        policy=AutoOrderPolicy(order_cadence_days=7),
        config=load_scenario_config(
            Path("config/assortment/display-auto-order-backtest-scenarios.json")
        ),
        date_from=start,
        date_to=second,
        keep_detail=True,
    )

    assert result.model["SKU-1"].order_qty == Decimal("7")
    assert result.model["SKU-1"].manual_order_lines == 1
    assert result.decision_rows[-1]["decision_trigger"] == "stockout_guard"
