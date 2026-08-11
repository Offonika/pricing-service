from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tasks.build_display_auto_order_dry_run import AutoOrderPolicy
from tasks.display_auto_order_backtest_preflight import (
    CarryingCostScenario,
    load_scenario_config,
)
from tasks.report_display_auto_order_frozen_backtest import (
    DemandAccelerationSignal,
    FrozenScenario,
    ServiceFloorCandidate,
    _free_initial_pipeline,
    acceleration_incremental_units,
    acceleration_pipeline_fraction,
    acceleration_segment_rule,
    allocate_service_floor_budget,
    apply_grow_target_protection,
    apply_quick_acceleration_guard,
    apply_quick_acceleration_segment_gates,
    apply_quick_base_pipeline_profiles,
    apply_service_floor_sku_cap,
    base_pipeline_fraction,
    base_pipeline_lot_risk_parameters,
    base_pipeline_margin_cost_ratio_floor,
    base_pipeline_profile_fractions,
    calculate_demand_acceleration,
    cap_acceleration_to_projected_shortage,
    combine_service_floor_with_economic_stock,
    empirical_underforecast_percentile,
    evaluate_acceleration_segment_gate,
    evaluate_acceleration_shortage_guard,
    historical_forecast_error_samples,
    release_open_acceleration_protection,
    risk_adjusted_base_pipeline_quantity,
    select_scenarios,
    simulate_scenario,
)


def _selection_scenario(scenario_id: str) -> FrozenScenario:
    return FrozenScenario(
        scenario_id=scenario_id,
        stage_profile="typical",
        kmp4_weight=Decimal("0.5"),
        cost=CarryingCostScenario(
            name="base",
            capital_annual_rate=Decimal("0.3"),
            storage_annual_rate=Decimal("0.1"),
            obsolescence_annual_rate=Decimal("0.25"),
        ),
    )


def test_quick_mode_selects_exactly_three_explicit_scenario_roles() -> None:
    scenarios = [
        _selection_scenario("control"),
        _selection_scenario("hypothesis"),
        _selection_scenario("cautious"),
        _selection_scenario("not-selected"),
    ]

    selection = select_scenarios(
        scenarios,
        run_mode="quick",
        control_scenario_id="control",
        hypothesis_scenario_id="hypothesis",
        cautious_scenario_id="cautious",
    )

    assert [scenario.scenario_id for scenario in selection.scenarios] == [
        "control",
        "hypothesis",
        "cautious",
    ]
    assert selection.base_scenario_id == "hypothesis"
    assert selection.control_scenario_id == "control"
    assert selection.scenario_roles == {
        "control": "control",
        "hypothesis": "hypothesis",
        "cautious": "cautious",
    }


@pytest.mark.parametrize(
    ("control", "hypothesis", "cautious", "message"),
    [
        ("control", "hypothesis", None, "requires scenario IDs"),
        ("control", "hypothesis", "hypothesis", "three different"),
        ("control", "missing", "cautious", "absent from frozen preflight"),
    ],
)
def test_quick_mode_rejects_incomplete_or_ambiguous_scenario_roles(
    control: str | None,
    hypothesis: str | None,
    cautious: str | None,
    message: str,
) -> None:
    scenarios = [
        _selection_scenario("control"),
        _selection_scenario("hypothesis"),
        _selection_scenario("cautious"),
    ]

    with pytest.raises(ValueError, match=message):
        select_scenarios(
            scenarios,
            run_mode="quick",
            control_scenario_id=control,
            hypothesis_scenario_id=hypothesis,
            cautious_scenario_id=cautious,
        )


def test_quick_acceleration_guard_overlays_hypothesis_and_cautious_only() -> None:
    selection = select_scenarios(
        [
            _selection_scenario("control"),
            _selection_scenario("hypothesis"),
            _selection_scenario("cautious"),
        ],
        run_mode="quick",
        control_scenario_id="control",
        hypothesis_scenario_id="hypothesis",
        cautious_scenario_id="cautious",
    )

    guarded = apply_quick_acceleration_guard(
        selection,
        hypothesis_min_shortage_qty=Decimal("2"),
        cautious_min_shortage_qty=Decimal("3"),
    )

    assert guarded.scenarios[0].scenario_id == "control"
    assert guarded.scenarios[1].scenario_id.endswith("_forecastguard_shortage2")
    assert guarded.scenarios[2].scenario_id.endswith("_forecastguard_shortage3")
    assert guarded.scenarios[1].grow_acceleration_require_forecast_growth is True
    assert guarded.scenarios[1].grow_acceleration_min_shortage_qty == Decimal("2")
    assert guarded.scenarios[2].grow_acceleration_min_shortage_qty == Decimal("3")
    assert guarded.base_scenario_id == guarded.scenarios[1].scenario_id


def test_quick_acceleration_guard_rejects_weaker_cautious_threshold() -> None:
    selection = select_scenarios(
        [
            _selection_scenario("control"),
            _selection_scenario("hypothesis"),
            _selection_scenario("cautious"),
        ],
        run_mode="quick",
        control_scenario_id="control",
        hypothesis_scenario_id="hypothesis",
        cautious_scenario_id="cautious",
    )

    with pytest.raises(ValueError, match="must not be lower"):
        apply_quick_acceleration_guard(
            selection,
            hypothesis_min_shortage_qty=Decimal("3"),
            cautious_min_shortage_qty=Decimal("2"),
        )


def test_quick_base_pipeline_profiles_clone_control_and_change_only_pipeline_trust() -> None:
    selection = select_scenarios(
        [
            _selection_scenario("control"),
            _selection_scenario("unrelated-hypothesis"),
            _selection_scenario("unrelated-cautious"),
        ],
        run_mode="quick",
        control_scenario_id="control",
        hypothesis_scenario_id="unrelated-hypothesis",
        cautious_scenario_id="unrelated-cautious",
    )

    result = apply_quick_base_pipeline_profiles(
        selection,
        hypothesis_profile="medium_90",
        cautious_profile="medium_95",
    )

    control, hypothesis, cautious = result.scenarios
    assert control.scenario_id == "control"
    assert hypothesis.stage_profile == control.stage_profile
    assert hypothesis.grow_acceleration_profile == "off"
    assert hypothesis.base_pipeline_medium_fraction == Decimal("0.90")
    assert cautious.base_pipeline_medium_fraction == Decimal("0.95")
    assert result.base_scenario_id == hypothesis.scenario_id


def test_base_pipeline_fraction_is_bounded_and_confidence_specific() -> None:
    assert base_pipeline_profile_fractions("medium_90") == (
        Decimal("1"),
        Decimal("0.90"),
        Decimal("0.50"),
    )
    assert base_pipeline_fraction(
        "high",
        high_fraction=Decimal("1.2"),
        medium_fraction=Decimal("0.9"),
        low_fraction=Decimal("0.5"),
    ) == Decimal("1")
    assert base_pipeline_fraction(
        "medium",
        high_fraction=Decimal("1"),
        medium_fraction=Decimal("0.9"),
        low_fraction=Decimal("0.5"),
    ) == Decimal("0.9")
    assert base_pipeline_fraction(
        "unknown",
        high_fraction=Decimal("1"),
        medium_fraction=Decimal("0.9"),
        low_fraction=Decimal("-1"),
    ) == Decimal("0")


def test_segmented_base_pipeline_profile_uses_current_margin_cost_boundary() -> None:
    assert base_pipeline_profile_fractions("medium_95_margin_cost_050") == (
        Decimal("1"),
        Decimal("0.95"),
        Decimal("1"),
    )
    assert base_pipeline_margin_cost_ratio_floor("medium_95_margin_cost_050") == Decimal("0.5")
    common = {
        "high_fraction": Decimal("1"),
        "medium_fraction": Decimal("0.95"),
        "low_fraction": Decimal("1"),
        "min_margin_to_cost_ratio": Decimal("0.5"),
        "inventory_cost_per_unit_rub": Decimal("100"),
    }

    assert base_pipeline_fraction(
        "medium", gross_margin_per_unit_rub=Decimal("50"), **common
    ) == Decimal("0.95")
    assert base_pipeline_fraction(
        "medium", gross_margin_per_unit_rub=Decimal("49.99"), **common
    ) == Decimal("1")
    assert base_pipeline_fraction(
        "high", gross_margin_per_unit_rub=Decimal("0"), **common
    ) == Decimal("1")


def test_lot_risk_profiles_use_p50_or_p75_with_ninety_percent_fraction() -> None:
    assert base_pipeline_lot_risk_parameters("medium_95_margin_cost_050_lotrisk_p50") == (
        "p50",
        Decimal("0.90"),
    )
    assert base_pipeline_lot_risk_parameters("medium_95_margin_cost_050_lotrisk_p75") == (
        "p75",
        Decimal("0.90"),
    )


def test_lot_risk_is_strictly_beyond_boundary_and_blends_per_lot() -> None:
    as_of = date(2026, 3, 1)
    effective, risky = risk_adjusted_base_pipeline_quantity(
        total_pipeline_qty=Decimal("6.4"),
        base_fraction=Decimal("0.95"),
        arrivals={
            as_of + timedelta(days=5): {"SKU-1": Decimal("3")},
            as_of + timedelta(days=6): {"SKU-1": Decimal("3.4")},
        },
        code="SKU-1",
        as_of=as_of,
        boundary_days=5,
        risk_fraction=Decimal("0.90"),
    )

    assert risky == Decimal("3.4")
    assert effective == Decimal("5.910")


def _run_lot_risk_scenario(
    *,
    profile: str,
    future_p50_days: int | None = None,
) -> object:
    start = date(2026, 3, 1)
    dates = [start]
    if future_p50_days is not None:
        dates.append(start + timedelta(days=1))
    facts = {
        business_date: [
            {
                "nomenclature_code": "SKU-1",
                "status": "sale",
                "physical_stock_qty": "0",
                "observed_sales_qty": "0",
                "effective_reserve_qty": "0",
                "placed_incoming_qty": "0",
            }
        ]
        for business_date in dates
    }
    decisions = {
        business_date: [
            {
                "decision_date": business_date.isoformat(),
                "nomenclature_code": "SKU-1",
                "scheduled_review": "1",
                "status": "sale",
                "forecast_rate_sales": "1.01",
                "lead_time_p50_days": str(5 if business_date == start else future_p50_days),
                "lead_time_p75_days": "7",
                "lead_time_confidence": "medium",
                "inventory_cost_per_unit_rub": "100",
                "gross_margin_per_unit_rub": "50",
            }
        ]
        for business_date in dates
    }
    high, medium, low = base_pipeline_profile_fractions(profile)
    boundary, risk_fraction = base_pipeline_lot_risk_parameters(profile)
    return simulate_scenario(
        scenario=FrozenScenario(
            scenario_id=profile,
            stage_profile="typical",
            kmp4_weight=Decimal("0"),
            cost=CarryingCostScenario(
                name="base",
                capital_annual_rate=Decimal("0.3"),
                storage_annual_rate=Decimal("0.1"),
                obsolescence_annual_rate=Decimal("0.25"),
            ),
            base_pipeline_profile=profile,
            base_pipeline_high_fraction=high,
            base_pipeline_medium_fraction=medium,
            base_pipeline_low_fraction=low,
            base_pipeline_min_margin_to_cost_ratio=(base_pipeline_margin_cost_ratio_floor(profile)),
            base_pipeline_lot_risk_boundary=boundary,
            base_pipeline_lot_risk_fraction=risk_fraction,
        ),
        fact_rows_by_date=facts,
        decision_rows_by_date=decisions,
        initial_pipeline_rows=[
            {
                "nomenclature_code": "SKU-1",
                "arrival_at": (start + timedelta(days=6)).isoformat(),
                "quantity": "6.5",
            }
        ],
        sales_by_code={"SKU-1": {}},
        policy=AutoOrderPolicy(order_cadence_days=7),
        config=load_scenario_config(
            Path("config/assortment/display-auto-order-backtest-scenarios.json")
        ),
        date_from=start,
        date_to=dates[-1],
        keep_detail=True,
        demand_sample_cache={},
    )


def test_lot_risk_profile_can_order_when_v15_does_not() -> None:
    v15 = _run_lot_risk_scenario(profile="medium_95_margin_cost_050")
    p50 = _run_lot_risk_scenario(profile="medium_95_margin_cost_050_lotrisk_p50")

    assert v15.model["SKU-1"].order_qty == Decimal("0")
    assert p50.model["SKU-1"].order_qty > Decimal("0")
    assert p50.decision_rows[0]["base_pipeline_lot_risk_boundary"] == "p50"
    assert p50.decision_rows[0]["base_pipeline_lot_risk_boundary_days"] == 5
    assert p50.decision_rows[0]["base_pipeline_lot_risky_qty"] == "6.5"
    assert p50.decision_rows[0]["effective_model_pipeline_qty"] == "5.850"


def test_lot_risk_uses_no_future_decision_boundary() -> None:
    short_future = _run_lot_risk_scenario(
        profile="medium_95_margin_cost_050_lotrisk_p50",
        future_p50_days=1,
    )
    long_future = _run_lot_risk_scenario(
        profile="medium_95_margin_cost_050_lotrisk_p50",
        future_p50_days=100,
    )

    assert short_future.decision_rows[0] == long_future.decision_rows[0]


def test_lot_risk_cannot_be_combined_with_acceleration() -> None:
    with pytest.raises(
        ValueError,
        match="base pipeline lot risk cannot be combined with acceleration",
    ):
        simulate_scenario(
            scenario=FrozenScenario(
                scenario_id="invalid-combination",
                stage_profile="typical",
                kmp4_weight=Decimal("0"),
                cost=CarryingCostScenario(
                    name="base",
                    capital_annual_rate=Decimal("0.3"),
                    storage_annual_rate=Decimal("0.1"),
                    obsolescence_annual_rate=Decimal("0.25"),
                ),
                grow_acceleration_profile="balanced",
                base_pipeline_lot_risk_boundary="p50",
                base_pipeline_lot_risk_fraction=Decimal("0.90"),
            ),
            fact_rows_by_date={},
            decision_rows_by_date={},
            initial_pipeline_rows=[],
            sales_by_code={},
            policy=AutoOrderPolicy(order_cadence_days=7),
            config=None,
            date_from=date(2026, 3, 1),
            date_to=date(2026, 3, 1),
            keep_detail=False,
        )


@pytest.mark.parametrize("cost", [Decimal("0"), Decimal("-1")])
def test_segmented_base_pipeline_unknown_or_nonpositive_cost_does_not_pass(
    cost: Decimal,
) -> None:
    assert base_pipeline_fraction(
        "medium",
        high_fraction=Decimal("1"),
        medium_fraction=Decimal("0.95"),
        low_fraction=Decimal("1"),
        min_margin_to_cost_ratio=Decimal("0.5"),
        gross_margin_per_unit_rub=Decimal("100"),
        inventory_cost_per_unit_rub=cost,
    ) == Decimal("1")


def test_segmented_base_pipeline_uses_only_economics_known_on_each_decision_date() -> None:
    start = date(2026, 3, 1)
    next_day = start + timedelta(days=1)
    facts = {
        day: [
            {
                "nomenclature_code": "SKU-1",
                "status": "sale",
                "physical_stock_qty": "0",
                "observed_sales_qty": "0",
                "effective_reserve_qty": "0",
                "placed_incoming_qty": "0",
            }
        ]
        for day in (start, next_day)
    }
    decisions = {
        start: [
            {
                "decision_date": start.isoformat(),
                "nomenclature_code": "SKU-1",
                "scheduled_review": "1",
                "status": "sale",
                "forecast_rate_sales": "1.01",
                "lead_time_p50_days": "5",
                "lead_time_p75_days": "5",
                "lead_time_confidence": "medium",
                "inventory_cost_per_unit_rub": "100",
                "gross_margin_per_unit_rub": "49",
            }
        ],
        next_day: [
            {
                "decision_date": next_day.isoformat(),
                "nomenclature_code": "SKU-1",
                "scheduled_review": "1",
                "status": "sale",
                "forecast_rate_sales": "1.01",
                "lead_time_p50_days": "5",
                "lead_time_p75_days": "5",
                "lead_time_confidence": "medium",
                "inventory_cost_per_unit_rub": "100",
                "gross_margin_per_unit_rub": "50",
            }
        ],
    }
    result = simulate_scenario(
        scenario=FrozenScenario(
            scenario_id="segmented-pipeline",
            stage_profile="typical",
            kmp4_weight=Decimal("0"),
            cost=CarryingCostScenario(
                name="base",
                capital_annual_rate=Decimal("0.3"),
                storage_annual_rate=Decimal("0.1"),
                obsolescence_annual_rate=Decimal("0.25"),
            ),
            base_pipeline_profile="medium_95_margin_cost_050",
            base_pipeline_medium_fraction=Decimal("0.95"),
            base_pipeline_min_margin_to_cost_ratio=Decimal("0.5"),
        ),
        fact_rows_by_date=facts,
        decision_rows_by_date=decisions,
        initial_pipeline_rows=[
            {
                "nomenclature_code": "SKU-1",
                "arrival_at": (start + timedelta(days=5)).isoformat(),
                "quantity": "6.5",
            }
        ],
        sales_by_code={"SKU-1": {}},
        policy=AutoOrderPolicy(order_cadence_days=7),
        config=load_scenario_config(
            Path("config/assortment/display-auto-order-backtest-scenarios.json")
        ),
        date_from=start,
        date_to=next_day,
        keep_detail=True,
        demand_sample_cache={},
    )

    assert [row["base_pipeline_fraction"] for row in result.decision_rows] == ["1", "0.95"]
    assert result.decision_rows[0]["recommended_order_qty"] == "0"


def test_base_pipeline_haircut_can_trigger_ordinary_min_max_order() -> None:
    start = date(2026, 3, 1)
    facts = {
        start: [
            {
                "nomenclature_code": "SKU-1",
                "status": "sale",
                "physical_stock_qty": "0",
                "observed_sales_qty": "0",
                "effective_reserve_qty": "0",
                "placed_incoming_qty": "0",
            }
        ]
    }
    decisions = {
        start: [
            {
                "decision_date": start.isoformat(),
                "nomenclature_code": "SKU-1",
                "scheduled_review": "1",
                "status": "sale",
                "forecast_rate_sales": "1.01",
                "lead_time_p50_days": "5",
                "lead_time_p75_days": "5",
                "lead_time_confidence": "medium",
                "inventory_cost_per_unit_rub": "100",
                "gross_margin_per_unit_rub": "50",
            }
        ]
    }
    initial_pipeline = [
        {
            "nomenclature_code": "SKU-1",
            "arrival_at": (start + timedelta(days=5)).isoformat(),
            "quantity": "6.5",
        }
    ]

    def run(medium_fraction: Decimal) -> object:
        return simulate_scenario(
            scenario=FrozenScenario(
                scenario_id=f"pipeline-{medium_fraction}",
                stage_profile="typical",
                kmp4_weight=Decimal("0"),
                cost=CarryingCostScenario(
                    name="base",
                    capital_annual_rate=Decimal("0.3"),
                    storage_annual_rate=Decimal("0.1"),
                    obsolescence_annual_rate=Decimal("0.25"),
                ),
                base_pipeline_profile="test",
                base_pipeline_medium_fraction=medium_fraction,
            ),
            fact_rows_by_date=facts,
            decision_rows_by_date=decisions,
            initial_pipeline_rows=initial_pipeline,
            sales_by_code={"SKU-1": {}},
            policy=AutoOrderPolicy(order_cadence_days=7),
            config=load_scenario_config(
                Path("config/assortment/display-auto-order-backtest-scenarios.json")
            ),
            date_from=start,
            date_to=start,
            keep_detail=True,
            demand_sample_cache={},
        )

    control = run(Decimal("1"))
    challenger = run(Decimal("0.9"))

    assert control.model["SKU-1"].order_qty == Decimal("0")
    assert challenger.model["SKU-1"].order_qty > Decimal("0")
    assert challenger.decision_rows[0]["base_pipeline_fraction"] == "0.9"


def test_quick_acceleration_guard_can_cap_add_on_by_projected_shortage() -> None:
    selection = select_scenarios(
        [
            _selection_scenario("control"),
            _selection_scenario("hypothesis"),
            _selection_scenario("cautious"),
        ],
        run_mode="quick",
        control_scenario_id="control",
        hypothesis_scenario_id="hypothesis",
        cautious_scenario_id="cautious",
    )

    guarded = apply_quick_acceleration_guard(
        selection,
        hypothesis_min_shortage_qty=Decimal("2"),
        cautious_min_shortage_qty=Decimal("3"),
        cap_to_projected_shortage=True,
    )

    assert guarded.scenarios[1].scenario_id.endswith("_forecastguard_shortage2_shortagecap")
    assert guarded.scenarios[1].grow_acceleration_cap_to_projected_shortage is True
    assert guarded.scenarios[1].grow_acceleration_quantity_policy == (
        "projected_shortage_capped_forecast_guard_no_economic_cap"
    )


def test_quick_acceleration_guard_can_enable_single_open_lot() -> None:
    selection = select_scenarios(
        [
            _selection_scenario("control"),
            _selection_scenario("hypothesis"),
            _selection_scenario("cautious"),
        ],
        run_mode="quick",
        control_scenario_id="control",
        hypothesis_scenario_id="hypothesis",
        cautious_scenario_id="cautious",
    )

    guarded = apply_quick_acceleration_guard(
        selection,
        hypothesis_min_shortage_qty=Decimal("2"),
        cautious_min_shortage_qty=Decimal("3"),
        cap_to_projected_shortage=True,
        single_open_lot=True,
    )

    assert guarded.scenarios[0].grow_acceleration_single_open_lot is False
    assert guarded.scenarios[1].scenario_id.endswith("_shortagecap_singleopenlot")
    assert guarded.scenarios[1].grow_acceleration_single_open_lot is True
    assert guarded.scenarios[1].grow_acceleration_quantity_policy.endswith("_single_open_lot")


def test_single_open_lot_requires_shortage_cap() -> None:
    selection = select_scenarios(
        [
            _selection_scenario("control"),
            _selection_scenario("hypothesis"),
            _selection_scenario("cautious"),
        ],
        run_mode="quick",
        control_scenario_id="control",
        hypothesis_scenario_id="hypothesis",
        cautious_scenario_id="cautious",
    )

    with pytest.raises(ValueError, match="requires projected shortage cap"):
        apply_quick_acceleration_guard(
            selection,
            hypothesis_min_shortage_qty=Decimal("2"),
            cautious_min_shortage_qty=Decimal("3"),
            single_open_lot=True,
        )


def test_quick_segment_gates_overlay_non_control_roles() -> None:
    selection = select_scenarios(
        [
            _selection_scenario("control"),
            _selection_scenario("hypothesis"),
            _selection_scenario("cautious"),
        ],
        run_mode="quick",
        control_scenario_id="control",
        hypothesis_scenario_id="hypothesis",
        cautious_scenario_id="cautious",
    )
    guarded = apply_quick_acceleration_guard(
        selection,
        hypothesis_min_shortage_qty=Decimal("2"),
        cautious_min_shortage_qty=Decimal("3"),
        cap_to_projected_shortage=True,
        single_open_lot=True,
    )

    gated = apply_quick_acceleration_segment_gates(
        guarded,
        hypothesis_profile="low_cost_high_confidence",
        cautious_profile="low_cost_high_confidence_sparse",
    )

    assert gated.scenarios[0].scenario_id == "control"
    assert gated.scenarios[1].grow_acceleration_max_unit_cost_rub == Decimal("500")
    assert gated.scenarios[1].grow_acceleration_allowed_demand_patterns == ()
    assert gated.scenarios[2].grow_acceleration_allowed_demand_patterns == (
        "intermittent",
        "lumpy",
    )
    assert gated.scenarios[2].grow_acceleration_max_p75_days == 90


def test_acceleration_segment_profile_and_gate_are_explicit() -> None:
    rule = acceleration_segment_rule("low_cost_high_confidence_sparse")
    assert rule.allowed_demand_patterns == ("intermittent", "lumpy")

    eligible = evaluate_acceleration_segment_gate(
        demand_pattern="intermittent",
        unit_cost_rub=Decimal("499.99"),
        lead_time_confidence="high",
        lead_time_p75_days=90,
        allowed_demand_patterns=rule.allowed_demand_patterns,
        max_unit_cost_rub=rule.max_unit_cost_rub,
        allowed_lead_confidences=rule.allowed_lead_confidences,
        max_p75_days=rule.max_p75_days,
    )
    blocked = evaluate_acceleration_segment_gate(
        demand_pattern="smooth",
        unit_cost_rub=Decimal("500"),
        lead_time_confidence="medium",
        lead_time_p75_days=91,
        allowed_demand_patterns=rule.allowed_demand_patterns,
        max_unit_cost_rub=rule.max_unit_cost_rub,
        allowed_lead_confidences=rule.allowed_lead_confidences,
        max_p75_days=rule.max_p75_days,
    )

    assert eligible.eligible is True
    assert blocked.eligible is False
    assert blocked.pattern_passed is False
    assert blocked.cost_passed is False
    assert blocked.confidence_passed is False
    assert blocked.p75_passed is False


def test_acceleration_uses_only_completed_past_days() -> None:
    as_of = date(2026, 3, 1)
    sales = {
        as_of - timedelta(days=20): Decimal("4"),
        as_of - timedelta(days=6): Decimal("1"),
        as_of - timedelta(days=1): Decimal("1"),
        as_of: Decimal("100"),
        as_of + timedelta(days=1): Decimal("1000"),
    }

    result = calculate_demand_acceleration(
        sales,
        as_of=as_of,
        recent_days=7,
        baseline_days=28,
        min_recent_sales=Decimal("2"),
        rate_multiplier=Decimal("1.5"),
    )

    assert result.recent_qty == Decimal("2")
    assert result.baseline_qty == Decimal("4")
    assert result.triggered is True


def test_acceleration_requires_minimum_recent_sales_and_rate_growth() -> None:
    as_of = date(2026, 3, 1)
    sales = {
        as_of - timedelta(days=20): Decimal("8"),
        as_of - timedelta(days=2): Decimal("1"),
    }

    result = calculate_demand_acceleration(
        sales,
        as_of=as_of,
        recent_days=14,
        baseline_days=42,
        min_recent_sales=Decimal("2"),
        rate_multiplier=Decimal("1.5"),
    )

    assert result.triggered is False


def test_acceleration_shortage_guard_requires_forecast_growth_and_p75_shortage() -> None:
    signal = DemandAccelerationSignal(
        recent_qty=Decimal("7"),
        baseline_qty=Decimal("2"),
        recent_rate=Decimal("0.5"),
        baseline_rate=Decimal("0.1"),
        rate_ratio=Decimal("5"),
        triggered=True,
    )

    eligible = evaluate_acceleration_shortage_guard(
        signal=signal,
        forecast_rate=Decimal("0.4"),
        lead_time_days=10,
        model_stock_qty=Decimal("3"),
        effective_reserve_qty=Decimal("0"),
        effective_pipeline_qty=Decimal("0"),
        require_forecast_growth=True,
        min_shortage_qty=Decimal("2"),
    )
    forecast_blocked = evaluate_acceleration_shortage_guard(
        signal=signal,
        forecast_rate=Decimal("0.5"),
        lead_time_days=10,
        model_stock_qty=Decimal("3"),
        effective_reserve_qty=Decimal("0"),
        effective_pipeline_qty=Decimal("0"),
        require_forecast_growth=True,
        min_shortage_qty=Decimal("2"),
    )
    shortage_blocked = evaluate_acceleration_shortage_guard(
        signal=signal,
        forecast_rate=Decimal("0.4"),
        lead_time_days=10,
        model_stock_qty=Decimal("4"),
        effective_reserve_qty=Decimal("0"),
        effective_pipeline_qty=Decimal("0"),
        require_forecast_growth=True,
        min_shortage_qty=Decimal("2"),
    )

    assert eligible.projected_demand_qty == Decimal("5")
    assert eligible.projected_shortage_qty == Decimal("2")
    assert eligible.eligible is True
    assert forecast_blocked.forecast_growth_passed is False
    assert forecast_blocked.eligible is False
    assert shortage_blocked.projected_shortage_qty == Decimal("1")
    assert shortage_blocked.shortage_passed is False
    assert shortage_blocked.eligible is False


def test_single_open_guard_counts_ordinary_pipeline_once_and_open_lot_fully() -> None:
    signal = DemandAccelerationSignal(
        recent_qty=Decimal("14"),
        baseline_qty=Decimal("1"),
        recent_rate=Decimal("1"),
        baseline_rate=Decimal("0.05"),
        rate_ratio=Decimal("20"),
        triggered=True,
    )

    guard = evaluate_acceleration_shortage_guard(
        signal=signal,
        forecast_rate=Decimal("0.1"),
        lead_time_days=10,
        model_stock_qty=Decimal("0"),
        effective_reserve_qty=Decimal("0"),
        effective_pipeline_qty=Decimal("2"),
        open_acceleration_protection_qty=Decimal("3"),
        require_forecast_growth=True,
        min_shortage_qty=Decimal("2"),
    )

    assert guard.gross_projected_shortage_qty == Decimal("8")
    assert guard.projected_shortage_qty == Decimal("5")
    assert guard.inventory_position_qty == Decimal("5")
    assert guard.open_acceleration_protection_qty == Decimal("3")


def test_open_acceleration_protection_releases_arrival_or_cancellation_only() -> None:
    assert release_open_acceleration_protection(Decimal("5"), arrived_qty=Decimal("2")) == Decimal(
        "3"
    )
    assert release_open_acceleration_protection(
        Decimal("5"), cancelled_qty=Decimal("7")
    ) == Decimal("0")
    assert release_open_acceleration_protection(
        Decimal("5"), arrived_qty=Decimal("-10")
    ) == Decimal("5")


def test_acceleration_add_on_is_capped_by_projected_shortage() -> None:
    assert cap_acceleration_to_projected_shortage(
        Decimal("8"),
        projected_shortage_qty=Decimal("3"),
        enabled=True,
    ) == Decimal("3")
    assert cap_acceleration_to_projected_shortage(
        Decimal("2"),
        projected_shortage_qty=Decimal("3"),
        enabled=True,
    ) == Decimal("2")
    assert cap_acceleration_to_projected_shortage(
        Decimal("8"),
        projected_shortage_qty=Decimal("3"),
        enabled=False,
    ) == Decimal("8")


def test_acceleration_increment_protects_p90_and_does_not_use_economic_cap() -> None:
    signal = DemandAccelerationSignal(
        recent_qty=Decimal("4"),
        baseline_qty=Decimal("1"),
        recent_rate=Decimal("0.5"),
        baseline_rate=Decimal("0.05"),
        rate_ratio=Decimal("10"),
        triggered=True,
    )

    assert acceleration_incremental_units(
        signal=signal,
        forecast_rate=Decimal("0.1"),
        coverage_days=20,
        percentile_safety_units=Decimal("2"),
        ordinary_safety_units=Decimal("0"),
        max_units=1000,
    ) == Decimal("8")


def test_acceleration_increment_only_adds_above_existing_ordinary_safety() -> None:
    signal = DemandAccelerationSignal(
        recent_qty=Decimal("4"),
        baseline_qty=Decimal("1"),
        recent_rate=Decimal("0.5"),
        baseline_rate=Decimal("0.05"),
        rate_ratio=Decimal("10"),
        triggered=True,
    )

    assert acceleration_incremental_units(
        signal=signal,
        forecast_rate=Decimal("0.1"),
        coverage_days=20,
        percentile_safety_units=Decimal("2"),
        ordinary_safety_units=Decimal("6"),
        max_units=1000,
    ) == Decimal("2")


def test_acceleration_pipeline_haircut_follows_lead_time_confidence() -> None:
    assert acceleration_pipeline_fraction(
        "high",
        medium_fraction=Decimal("0.75"),
        low_fraction=Decimal("0.5"),
    ) == Decimal("1")
    assert acceleration_pipeline_fraction(
        "medium",
        medium_fraction=Decimal("0.75"),
        low_fraction=Decimal("0.5"),
    ) == Decimal("0.75")
    assert acceleration_pipeline_fraction(
        "low",
        medium_fraction=Decimal("0.75"),
        low_fraction=Decimal("0.5"),
    ) == Decimal("0.5")


def test_acceleration_review_uses_past_sales_and_pipeline_confidence() -> None:
    start = date(2026, 3, 1)
    second = start + timedelta(days=1)
    cost = CarryingCostScenario(
        name="base",
        capital_annual_rate=Decimal("0.3"),
        storage_annual_rate=Decimal("0.1"),
        obsolescence_annual_rate=Decimal("0.25"),
    )
    facts = {
        start: [
            {
                "nomenclature_code": "SKU-1",
                "status": "sale",
                "physical_stock_qty": "0",
                "observed_sales_qty": "1",
                "placed_incoming_qty": "0",
            }
        ],
        second: [
            {
                "nomenclature_code": "SKU-1",
                "status": "sale",
                "physical_stock_qty": "0",
                "observed_sales_qty": "0",
                "placed_incoming_qty": "0",
            }
        ],
    }
    decisions = {
        start: [
            {
                "decision_date": start.isoformat(),
                "nomenclature_code": "SKU-1",
                "scheduled_review": "1",
                "status": "sale",
                "forecast_rate_sales": "0.01",
                "lead_time_p50_days": "5",
                "lead_time_p75_days": "5",
                "inventory_cost_per_unit_rub": "1",
                "gross_margin_per_unit_rub": "100",
            }
        ]
    }
    sales = {
        "SKU-1": {
            start - timedelta(days=6): Decimal("1"),
            start - timedelta(days=4): Decimal("1"),
            start - timedelta(days=2): Decimal("1"),
            start: Decimal("1"),
        }
    }
    samples = [Decimal("0")] * 6 + [Decimal("10")] * 2
    sample_cache = {("SKU-1", business_date, 6): samples for business_date in (start, second)}

    def run(confidence: str) -> object:
        decisions[start][0]["lead_time_confidence"] = confidence
        return simulate_scenario(
            scenario=FrozenScenario(
                scenario_id=f"grow_accel_{confidence}",
                stage_profile="typical",
                kmp4_weight=Decimal("0"),
                cost=cost,
                forecast_error_percentile=Decimal("0.75"),
                grow_acceleration_profile="fast",
                grow_acceleration_recent_days=7,
                grow_acceleration_baseline_days=28,
                grow_acceleration_min_recent_sales=Decimal("4"),
                grow_acceleration_rate_multiplier=Decimal("1.5"),
                grow_acceleration_sku_cap_rub=Decimal("50000"),
                grow_acceleration_stage_budget_rub=Decimal("8000000"),
                grow_acceleration_medium_pipeline_fraction=Decimal("0.75"),
                grow_acceleration_low_pipeline_fraction=Decimal("0.5"),
            ),
            fact_rows_by_date=facts,
            decision_rows_by_date=decisions,
            initial_pipeline_rows=[
                {
                    "nomenclature_code": "SKU-1",
                    "arrival_at": (second + timedelta(days=10)).isoformat(),
                    "quantity": "5",
                }
            ],
            sales_by_code=sales,
            policy=AutoOrderPolicy(order_cadence_days=1),
            config=load_scenario_config(
                Path("config/assortment/display-auto-order-backtest-scenarios.json")
            ),
            date_from=start,
            date_to=second,
            keep_detail=True,
            demand_sample_cache=dict(sample_cache),
        )

    high = run("high")
    medium = run("medium")
    low = run("low")

    assert high.model["SKU-1"].order_qty == Decimal("0")
    assert medium.model["SKU-1"].order_qty == Decimal("2")
    assert low.model["SKU-1"].order_qty == Decimal("3")
    assert medium.decision_rows[-1]["decision_date"] == second.isoformat()
    assert medium.decision_rows[-1]["decision_trigger"] == "acceleration_review"
    assert medium.decision_rows[-1]["acceleration_allocated_qty"] == "4"
    assert medium.decision_rows[-1]["acceleration_pipeline_fraction"] == "0.75"


def _run_single_open_acceleration_scenario(
    *,
    days: int,
    shortage_growth: bool = False,
    unit_cost_rub: Decimal = Decimal("1"),
    lead_time_confidence: str = "high",
    segment_profile: str = "off",
    allowed_demand_patterns: tuple[str, ...] = (),
    max_unit_cost_rub: Decimal = Decimal("0"),
    allowed_lead_confidences: tuple[str, ...] = (),
    max_p75_days: int = 0,
) -> object:
    start = date(2026, 3, 1)
    cost = CarryingCostScenario(
        name="base",
        capital_annual_rate=Decimal("0.3"),
        storage_annual_rate=Decimal("0.1"),
        obsolescence_annual_rate=Decimal("0.25"),
    )
    fact_rows: dict[date, list[dict[str, str]]] = {}
    decision_rows: dict[date, list[dict[str, str]]] = {}
    sales = {
        start - timedelta(days=6): Decimal("1"),
        start - timedelta(days=4): Decimal("1"),
        start - timedelta(days=2): Decimal("1"),
        start - timedelta(days=1): Decimal("1"),
    }
    sample_cache: dict[tuple[str, date, int], list[Decimal]] = {}
    for offset in range(days):
        business_date = start + timedelta(days=offset)
        observed = Decimal("5") if shortage_growth and offset == 1 else Decimal("0")
        if observed > Decimal("0"):
            sales[business_date] = observed
        fact_rows[business_date] = [
            {
                "nomenclature_code": "SKU-1",
                "status": "sale",
                "physical_stock_qty": "0",
                "observed_sales_qty": str(observed),
                "placed_incoming_qty": "0",
                "effective_reserve_qty": "0",
            }
        ]
        decision_rows[business_date] = [
            {
                "decision_date": business_date.isoformat(),
                "nomenclature_code": "SKU-1",
                "scheduled_review": "1",
                "status": "sale",
                "forecast_rate_sales": "0.1",
                "lead_time_p50_days": "5",
                "lead_time_p75_days": "5",
                "lead_time_confidence": lead_time_confidence,
                "inventory_cost_per_unit_rub": str(unit_cost_rub),
                "gross_margin_per_unit_rub": "100",
            }
        ]
        sample_cache[("SKU-1", business_date, 6)] = [Decimal("0")] * 8

    return simulate_scenario(
        scenario=FrozenScenario(
            scenario_id="grow_accel_single_open",
            stage_profile="typical",
            kmp4_weight=Decimal("0"),
            cost=cost,
            grow_weekly_reduction_cap=Decimal("0.2"),
            forecast_error_percentile=Decimal("0.9"),
            grow_acceleration_profile="balanced",
            grow_acceleration_quantity_policy="single_open_lot",
            grow_acceleration_recent_days=7,
            grow_acceleration_baseline_days=28,
            grow_acceleration_min_recent_sales=Decimal("4"),
            grow_acceleration_rate_multiplier=Decimal("1.5"),
            grow_acceleration_sku_cap_rub=Decimal("50000"),
            grow_acceleration_stage_budget_rub=Decimal("8000000"),
            grow_acceleration_require_forecast_growth=True,
            grow_acceleration_min_shortage_qty=Decimal("2"),
            grow_acceleration_cap_to_projected_shortage=True,
            grow_acceleration_single_open_lot=True,
            grow_acceleration_segment_profile=segment_profile,
            grow_acceleration_allowed_demand_patterns=allowed_demand_patterns,
            grow_acceleration_max_unit_cost_rub=max_unit_cost_rub,
            grow_acceleration_allowed_lead_confidences=allowed_lead_confidences,
            grow_acceleration_max_p75_days=max_p75_days,
        ),
        fact_rows_by_date=fact_rows,
        decision_rows_by_date=decision_rows,
        initial_pipeline_rows=[],
        sales_by_code={"SKU-1": sales},
        policy=AutoOrderPolicy(order_cadence_days=1),
        config=load_scenario_config(
            Path("config/assortment/display-auto-order-backtest-scenarios.json")
        ),
        date_from=start,
        date_to=start + timedelta(days=days - 1),
        keep_detail=True,
        demand_sample_cache=sample_cache,
    )


def test_single_open_acceleration_blocks_repeat_for_unchanged_shortage() -> None:
    result = _run_single_open_acceleration_scenario(days=2)

    assert result.model["SKU-1"].order_qty == Decimal("4")
    assert result.diagnostics.acceleration_order_component_qty == Decimal("3")
    assert result.diagnostics.acceleration_single_open_blocked_recalculations == 1
    assert result.diagnostics.acceleration_open_protection_ending_qty == Decimal("3")
    assert result.decision_rows[0]["acceleration_order_component_qty"] == "3"
    assert result.decision_rows[1]["recommended_order_qty"] == "0"


def test_single_open_acceleration_orders_only_shortage_growth() -> None:
    result = _run_single_open_acceleration_scenario(days=3, shortage_growth=True)

    assert result.model["SKU-1"].order_qty == Decimal("6")
    assert result.diagnostics.acceleration_order_component_qty == Decimal("5")
    assert result.decision_rows[2]["acceleration_projected_shortage_to_p75_qty"] == "2"
    assert result.decision_rows[2]["acceleration_order_component_qty"] == "2"
    assert result.decision_rows[2]["acceleration_open_protection_before_qty"] == "3"
    assert result.decision_rows[2]["acceleration_open_protection_after_qty"] == "5"


def test_single_open_acceleration_releases_protection_on_arrival_not_before() -> None:
    before_arrival = _run_single_open_acceleration_scenario(days=5)
    on_arrival = _run_single_open_acceleration_scenario(days=6)

    assert before_arrival.diagnostics.acceleration_released_on_arrival_qty == Decimal("0")
    assert before_arrival.diagnostics.acceleration_open_protection_ending_qty == Decimal("3")
    assert on_arrival.diagnostics.acceleration_released_on_arrival_qty == Decimal("3")
    assert on_arrival.diagnostics.acceleration_open_protection_ending_qty == Decimal("0")


def test_single_open_acceleration_does_not_persist_in_grow_target_state() -> None:
    result = _run_single_open_acceleration_scenario(days=2)

    first, second = result.decision_rows
    assert first["ordinary_max_stock_qty"] == "1"
    assert first["max_stock_qty"] == "4"
    assert second["ordinary_max_stock_qty"] == "1"
    assert second["max_stock_qty"] == "1"


def test_segment_gate_blocks_acceleration_but_keeps_ordinary_min_max_order() -> None:
    result = _run_single_open_acceleration_scenario(
        days=1,
        unit_cost_rub=Decimal("500"),
        segment_profile="low_cost_high_confidence",
        max_unit_cost_rub=Decimal("500"),
        allowed_lead_confidences=("high",),
        max_p75_days=90,
    )

    assert result.model["SKU-1"].order_qty == Decimal("1")
    assert result.diagnostics.acceleration_guard_eligible_recalculations == 1
    assert result.diagnostics.acceleration_segment_blocked_recalculations == 1
    assert result.diagnostics.acceleration_segment_blocked_cost_recalculations == 1
    assert result.diagnostics.acceleration_order_component_qty == Decimal("0")


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


def test_service_floor_is_minimum_and_economic_stock_can_add_above_it() -> None:
    assert combine_service_floor_with_economic_stock(
        service_floor_units=Decimal("6"),
        economic_cap_units=Decimal("7"),
        economic_percentile_target_units=Decimal("8"),
    ) == Decimal("7")
    assert combine_service_floor_with_economic_stock(
        service_floor_units=Decimal("6"),
        economic_cap_units=Decimal("4"),
        economic_percentile_target_units=Decimal("8"),
    ) == Decimal("6")


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
        forecast_error_percentile=Decimal("0.9"),
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
