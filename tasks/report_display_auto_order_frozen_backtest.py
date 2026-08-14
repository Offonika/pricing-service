"""Run the next display auto-order backtest from a frozen PASS preflight only."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from app.services.assortment_lifecycle import AssortmentStatus
from app.services.display_auto_order_demand_pattern import (
    classify_demand_pattern,
    completed_weekly_demand,
)
from tasks.build_display_auto_order_dry_run import (
    AutoOrderPolicy,
    load_auto_order_policy,
    rounded_order_qty,
)
from tasks.display_auto_order_backtest_preflight import (
    CarryingCostScenario,
    calculate_economic_safety_stock,
    load_scenario_config,
    validate_preflight_directory,
)

ZERO = Decimal("0")
ONE = Decimal("1")
YEAR_DAYS = Decimal("365")
BASE_SCENARIO_ID = (
    "grow_accel_balanced_retimep90_nocap_r14_b42_x150_min2_stage8000000_"
    "cap20_hold4_typical_kmp0_5_sitebalanced_base"
)
CONTROL_SCENARIO_ID = "grow_cap20_p90_hold4_typical_kmp0_5_sitebalanced_base"
OUTPUT_SCHEMA = "display_auto_order_frozen_backtest.v13"
RUN_MODE_FULL = "full"
RUN_MODE_QUICK = "quick"
ACCELERATION_SEGMENT_PROFILE_OFF = "off"
ACCELERATION_SEGMENT_PROFILE_LOW_RISK = "low_cost_high_confidence"
ACCELERATION_SEGMENT_PROFILE_LOW_RISK_SPARSE = "low_cost_high_confidence_sparse"
ACCELERATION_SEGMENT_PROFILE_LOW_RISK_COLD_START = "low_cost_high_confidence_cold_start"
ACCELERATION_SEGMENT_PROFILES = (
    ACCELERATION_SEGMENT_PROFILE_OFF,
    ACCELERATION_SEGMENT_PROFILE_LOW_RISK,
    ACCELERATION_SEGMENT_PROFILE_LOW_RISK_SPARSE,
    ACCELERATION_SEGMENT_PROFILE_LOW_RISK_COLD_START,
)
BASE_PIPELINE_PROFILE_OFF = "off"
BASE_PIPELINE_PROFILE_MEDIUM_95 = "medium_95"
BASE_PIPELINE_PROFILE_MEDIUM_90 = "medium_90"
BASE_PIPELINE_PROFILE_MEDIUM_95_MARGIN_COST_050 = "medium_95_margin_cost_050"
BASE_PIPELINE_PROFILE_MEDIUM_95_MARGIN_COST_100 = "medium_95_margin_cost_100"
BASE_PIPELINE_PROFILE_MEDIUM_95_MARGIN_COST_050_LOT_RISK_P50 = (
    "medium_95_margin_cost_050_lotrisk_p50"
)
BASE_PIPELINE_PROFILE_MEDIUM_95_MARGIN_COST_050_LOT_RISK_P75 = (
    "medium_95_margin_cost_050_lotrisk_p75"
)
BASE_PIPELINE_PROFILES = (
    BASE_PIPELINE_PROFILE_OFF,
    BASE_PIPELINE_PROFILE_MEDIUM_95,
    BASE_PIPELINE_PROFILE_MEDIUM_90,
    BASE_PIPELINE_PROFILE_MEDIUM_95_MARGIN_COST_050,
    BASE_PIPELINE_PROFILE_MEDIUM_95_MARGIN_COST_100,
    BASE_PIPELINE_PROFILE_MEDIUM_95_MARGIN_COST_050_LOT_RISK_P50,
    BASE_PIPELINE_PROFILE_MEDIUM_95_MARGIN_COST_050_LOT_RISK_P75,
)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(_clean(value) or "0")
    except (ArithmeticError, ValueError):
        return ZERO


def _ceil(value: Decimal) -> Decimal:
    return value.to_integral_value(rounding=ROUND_CEILING)


def _date(value: Any) -> date | None:
    rendered = _clean(value)
    if not rendered:
        return None
    try:
        return date.fromisoformat(rendered[:10])
    except ValueError:
        return None


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8-sig")
        return
    columns: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                columns.append(key)
                seen.add(key)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class FrozenScenario:
    scenario_id: str
    stage_profile: str
    kmp4_weight: Decimal
    cost: CarryingCostScenario
    site_profile: str = "off"
    site_order_weight: Decimal = ZERO
    site_unordered_cart_weight: Decimal = ZERO
    grow_weekly_reduction_cap: Decimal = ZERO
    forecast_error_percentile: Decimal = ZERO
    grow_entry_protection_weeks: int = 0
    grow_service_floor_percentile: Decimal = ZERO
    grow_service_floor_sku_cap_rub: Decimal = ZERO
    grow_service_floor_stage_budget_rub: Decimal = ZERO
    grow_acceleration_profile: str = "off"
    grow_acceleration_quantity_policy: str = "off"
    grow_acceleration_recent_days: int = 0
    grow_acceleration_baseline_days: int = 0
    grow_acceleration_min_recent_sales: Decimal = ZERO
    grow_acceleration_rate_multiplier: Decimal = ZERO
    grow_acceleration_sku_cap_rub: Decimal = ZERO
    grow_acceleration_stage_budget_rub: Decimal = ZERO
    grow_acceleration_medium_pipeline_fraction: Decimal = ONE
    grow_acceleration_low_pipeline_fraction: Decimal = ONE
    grow_acceleration_require_forecast_growth: bool = False
    grow_acceleration_min_shortage_qty: Decimal = ZERO
    grow_acceleration_cap_to_projected_shortage: bool = False
    grow_acceleration_single_open_lot: bool = False
    grow_acceleration_segment_profile: str = ACCELERATION_SEGMENT_PROFILE_OFF
    grow_acceleration_allowed_demand_patterns: tuple[str, ...] = ()
    grow_acceleration_max_unit_cost_rub: Decimal = ZERO
    grow_acceleration_allowed_lead_confidences: tuple[str, ...] = ()
    grow_acceleration_max_p75_days: int = 0
    base_pipeline_profile: str = BASE_PIPELINE_PROFILE_OFF
    base_pipeline_high_fraction: Decimal = ONE
    base_pipeline_medium_fraction: Decimal = ONE
    base_pipeline_low_fraction: Decimal = ONE
    base_pipeline_min_margin_to_cost_ratio: Decimal = ZERO
    base_pipeline_lot_risk_boundary: str = ""
    base_pipeline_lot_risk_fraction: Decimal = ONE
    legacy: bool = False


@dataclass(frozen=True)
class HybridGapEvaluation:
    demand_rate: Decimal
    new_arrival_date: date | None
    reliable_arrival_date: date | None
    coverable_days: int
    stock_at_new_arrival_qty: Decimal
    coverable_demand_qty: Decimal
    coverable_shortage_qty: Decimal
    open_hybrid_qty: Decimal
    eligible: bool


@dataclass
class Metric:
    observed_demand_qty: Decimal = ZERO
    hidden_demand_qty: Decimal = ZERO
    hidden_kmp4_qty: Decimal = ZERO
    hidden_site_order_qty: Decimal = ZERO
    hidden_site_cart_qty: Decimal = ZERO
    hidden_reserve_backlog_qty: Decimal = ZERO
    served_observed_qty: Decimal = ZERO
    served_hidden_qty: Decimal = ZERO
    served_hidden_kmp4_qty: Decimal = ZERO
    served_hidden_site_order_qty: Decimal = ZERO
    served_hidden_site_cart_qty: Decimal = ZERO
    served_hidden_reserve_backlog_qty: Decimal = ZERO
    lost_observed_qty: Decimal = ZERO
    lost_hidden_qty: Decimal = ZERO
    gross_profit_rub: Decimal = ZERO
    inventory_qty_days: Decimal = ZERO
    priced_inventory_qty_days: Decimal = ZERO
    inventory_value_days_rub: Decimal = ZERO
    ending_inventory_qty: Decimal = ZERO
    ending_target_stock_qty: Decimal = ZERO
    order_qty: Decimal = ZERO
    order_value_rub: Decimal = ZERO
    order_lines: int = 0
    manual_order_lines: int = 0
    manual_review_created: int = 0
    manual_review_updated: int = 0
    safety_stock_units_ordered: Decimal = ZERO
    exogenous_launch_seed_qty: Decimal = ZERO

    @property
    def potential_demand_qty(self) -> Decimal:
        return self.observed_demand_qty + self.hidden_demand_qty

    @property
    def served_qty(self) -> Decimal:
        return self.served_observed_qty + self.served_hidden_qty

    @property
    def lost_qty(self) -> Decimal:
        return self.lost_observed_qty + self.lost_hidden_qty


@dataclass
class SimulationResult:
    scenario: FrozenScenario
    actual: dict[str, Metric]
    model: dict[str, Metric]
    actual_by_stage: dict[str, Metric]
    model_by_stage: dict[str, Metric]
    decision_rows: list[dict[str, Any]]
    daily_rows: list[dict[str, Any]]
    loss_rows: list[dict[str, Any]]
    diagnostics: ScenarioDiagnostics


@dataclass
class ScenarioDiagnostics:
    service_floor_positive_recalculations: int = 0
    service_floor_budget_limited_recalculations: int = 0
    service_floor_requested_qty: Decimal = ZERO
    service_floor_allocated_qty: Decimal = ZERO
    acceleration_triggered_recalculations: int = 0
    acceleration_forecast_growth_passed_recalculations: int = 0
    acceleration_shortage_passed_recalculations: int = 0
    acceleration_guard_eligible_recalculations: int = 0
    acceleration_positive_recalculations: int = 0
    acceleration_shortage_capped_recalculations: int = 0
    acceleration_budget_limited_recalculations: int = 0
    acceleration_uncapped_requested_qty: Decimal = ZERO
    acceleration_shortage_cap_reduction_qty: Decimal = ZERO
    acceleration_requested_qty: Decimal = ZERO
    acceleration_allocated_qty: Decimal = ZERO
    acceleration_single_open_blocked_recalculations: int = 0
    acceleration_single_open_blocked_qty: Decimal = ZERO
    acceleration_order_component_qty: Decimal = ZERO
    acceleration_released_on_arrival_qty: Decimal = ZERO
    acceleration_open_protection_peak_qty: Decimal = ZERO
    acceleration_open_protection_ending_qty: Decimal = ZERO
    acceleration_segment_evaluated_recalculations: int = 0
    acceleration_segment_passed_recalculations: int = 0
    acceleration_segment_blocked_recalculations: int = 0
    acceleration_segment_blocked_pattern_recalculations: int = 0
    acceleration_segment_blocked_cost_recalculations: int = 0
    acceleration_segment_blocked_confidence_recalculations: int = 0
    acceleration_segment_blocked_p75_recalculations: int = 0
    base_pipeline_lot_risk_evaluations: int = 0
    base_pipeline_lot_risk_positive_evaluations: int = 0
    base_pipeline_lot_risk_qty_evaluated: Decimal = ZERO
    base_pipeline_lot_risk_effective_reduction_qty: Decimal = ZERO
    decision_service_buffer_positive_decisions: int = 0
    decision_service_buffer_requested_qty: Decimal = ZERO
    hybrid_gap_evaluations: int = 0
    hybrid_gap_eligible_evaluations: int = 0
    hybrid_gap_positive_order_decisions: int = 0
    hybrid_gap_open_lot_blocked_evaluations: int = 0
    hybrid_gap_requested_qty: Decimal = ZERO
    hybrid_gap_order_component_qty: Decimal = ZERO
    hybrid_gap_released_on_arrival_qty: Decimal = ZERO
    hybrid_gap_acceleration_evaluations: int = 0
    hybrid_gap_acceleration_passed_evaluations: int = 0
    hybrid_gap_acceleration_blocked_evaluations: int = 0

    def as_summary_fields(self) -> dict[str, Any]:
        return {
            "service_floor_positive_recalculations": (self.service_floor_positive_recalculations),
            "service_floor_budget_limited_recalculations": (
                self.service_floor_budget_limited_recalculations
            ),
            "service_floor_requested_qty": str(self.service_floor_requested_qty),
            "service_floor_allocated_qty": str(self.service_floor_allocated_qty),
            "acceleration_triggered_recalculations": (self.acceleration_triggered_recalculations),
            "acceleration_forecast_growth_passed_recalculations": (
                self.acceleration_forecast_growth_passed_recalculations
            ),
            "acceleration_shortage_passed_recalculations": (
                self.acceleration_shortage_passed_recalculations
            ),
            "acceleration_guard_eligible_recalculations": (
                self.acceleration_guard_eligible_recalculations
            ),
            "acceleration_positive_recalculations": (self.acceleration_positive_recalculations),
            "acceleration_shortage_capped_recalculations": (
                self.acceleration_shortage_capped_recalculations
            ),
            "acceleration_budget_limited_recalculations": (
                self.acceleration_budget_limited_recalculations
            ),
            "acceleration_uncapped_requested_qty": str(self.acceleration_uncapped_requested_qty),
            "acceleration_shortage_cap_reduction_qty": str(
                self.acceleration_shortage_cap_reduction_qty
            ),
            "acceleration_requested_qty": str(self.acceleration_requested_qty),
            "acceleration_allocated_qty": str(self.acceleration_allocated_qty),
            "acceleration_single_open_blocked_recalculations": (
                self.acceleration_single_open_blocked_recalculations
            ),
            "acceleration_single_open_blocked_qty": str(self.acceleration_single_open_blocked_qty),
            "acceleration_order_component_qty": str(self.acceleration_order_component_qty),
            "acceleration_released_on_arrival_qty": str(self.acceleration_released_on_arrival_qty),
            "acceleration_open_protection_peak_qty": str(
                self.acceleration_open_protection_peak_qty
            ),
            "acceleration_open_protection_ending_qty": str(
                self.acceleration_open_protection_ending_qty
            ),
            "acceleration_segment_evaluated_recalculations": (
                self.acceleration_segment_evaluated_recalculations
            ),
            "acceleration_segment_passed_recalculations": (
                self.acceleration_segment_passed_recalculations
            ),
            "acceleration_segment_blocked_recalculations": (
                self.acceleration_segment_blocked_recalculations
            ),
            "acceleration_segment_blocked_pattern_recalculations": (
                self.acceleration_segment_blocked_pattern_recalculations
            ),
            "acceleration_segment_blocked_cost_recalculations": (
                self.acceleration_segment_blocked_cost_recalculations
            ),
            "acceleration_segment_blocked_confidence_recalculations": (
                self.acceleration_segment_blocked_confidence_recalculations
            ),
            "acceleration_segment_blocked_p75_recalculations": (
                self.acceleration_segment_blocked_p75_recalculations
            ),
            "base_pipeline_lot_risk_evaluations": self.base_pipeline_lot_risk_evaluations,
            "base_pipeline_lot_risk_positive_evaluations": (
                self.base_pipeline_lot_risk_positive_evaluations
            ),
            "base_pipeline_lot_risk_qty_evaluated": str(self.base_pipeline_lot_risk_qty_evaluated),
            "base_pipeline_lot_risk_effective_reduction_qty": str(
                self.base_pipeline_lot_risk_effective_reduction_qty
            ),
            "decision_service_buffer_positive_decisions": (
                self.decision_service_buffer_positive_decisions
            ),
            "decision_service_buffer_requested_qty": str(
                self.decision_service_buffer_requested_qty
            ),
            "hybrid_gap_evaluations": self.hybrid_gap_evaluations,
            "hybrid_gap_eligible_evaluations": self.hybrid_gap_eligible_evaluations,
            "hybrid_gap_positive_order_decisions": (self.hybrid_gap_positive_order_decisions),
            "hybrid_gap_open_lot_blocked_evaluations": (
                self.hybrid_gap_open_lot_blocked_evaluations
            ),
            "hybrid_gap_requested_qty": str(self.hybrid_gap_requested_qty),
            "hybrid_gap_order_component_qty": str(self.hybrid_gap_order_component_qty),
            "hybrid_gap_released_on_arrival_qty": str(self.hybrid_gap_released_on_arrival_qty),
            "hybrid_gap_acceleration_evaluations": (self.hybrid_gap_acceleration_evaluations),
            "hybrid_gap_acceleration_passed_evaluations": (
                self.hybrid_gap_acceleration_passed_evaluations
            ),
            "hybrid_gap_acceleration_blocked_evaluations": (
                self.hybrid_gap_acceleration_blocked_evaluations
            ),
        }


@dataclass(frozen=True)
class ScenarioSelection:
    run_mode: str
    scenarios: tuple[FrozenScenario, ...]
    base_scenario_id: str
    control_scenario_id: str
    scenario_roles: Mapping[str, str]


def select_scenarios(
    scenarios: Sequence[FrozenScenario],
    *,
    run_mode: str,
    control_scenario_id: str | None = None,
    hypothesis_scenario_id: str | None = None,
    cautious_scenario_id: str | None = None,
) -> ScenarioSelection:
    by_id: dict[str, FrozenScenario] = {}
    duplicate_ids: set[str] = set()
    for scenario in scenarios:
        if scenario.scenario_id in by_id:
            duplicate_ids.add(scenario.scenario_id)
        by_id[scenario.scenario_id] = scenario
    if duplicate_ids:
        rendered = ", ".join(sorted(duplicate_ids))
        raise ValueError(f"frozen scenario definitions contain duplicate IDs: {rendered}")

    role_values = {
        "control": _clean(control_scenario_id),
        "hypothesis": _clean(hypothesis_scenario_id),
        "cautious": _clean(cautious_scenario_id),
    }
    if run_mode == RUN_MODE_FULL:
        if any(role_values.values()):
            raise ValueError("scenario role arguments are supported only in quick mode")
        return ScenarioSelection(
            run_mode=run_mode,
            scenarios=tuple(scenarios),
            base_scenario_id=BASE_SCENARIO_ID,
            control_scenario_id=CONTROL_SCENARIO_ID,
            scenario_roles={
                "control": CONTROL_SCENARIO_ID,
                "hypothesis": BASE_SCENARIO_ID,
            },
        )
    if run_mode != RUN_MODE_QUICK:
        raise ValueError(f"unsupported run mode: {run_mode}")

    missing_roles = [role for role, scenario_id in role_values.items() if not scenario_id]
    if missing_roles:
        raise ValueError("quick mode requires scenario IDs for roles: " + ", ".join(missing_roles))
    if len(set(role_values.values())) != 3:
        raise ValueError("quick mode requires three different scenario IDs")
    missing_ids = [scenario_id for scenario_id in role_values.values() if scenario_id not in by_id]
    if missing_ids:
        raise ValueError(
            "quick scenario IDs are absent from frozen preflight: " + ", ".join(missing_ids)
        )

    return ScenarioSelection(
        run_mode=run_mode,
        scenarios=tuple(by_id[role_values[role]] for role in ("control", "hypothesis", "cautious")),
        base_scenario_id=role_values["hypothesis"],
        control_scenario_id=role_values["control"],
        scenario_roles=role_values,
    )


def apply_quick_acceleration_guard(
    selection: ScenarioSelection,
    *,
    hypothesis_min_shortage_qty: Decimal,
    cautious_min_shortage_qty: Decimal,
    cap_to_projected_shortage: bool = False,
    single_open_lot: bool = False,
) -> ScenarioSelection:
    if selection.run_mode != RUN_MODE_QUICK:
        raise ValueError("acceleration guard overlay is supported only in quick mode")
    hypothesis_threshold = _decimal(hypothesis_min_shortage_qty)
    cautious_threshold = _decimal(cautious_min_shortage_qty)
    if hypothesis_threshold <= ZERO or cautious_threshold <= ZERO:
        raise ValueError("quick acceleration guard shortage thresholds must be positive")
    if cautious_threshold < hypothesis_threshold:
        raise ValueError("cautious shortage threshold must not be lower than hypothesis threshold")
    if single_open_lot and not cap_to_projected_shortage:
        raise ValueError("single open acceleration lot requires projected shortage cap")

    source_by_id = {scenario.scenario_id: scenario for scenario in selection.scenarios}
    guarded_by_role: dict[str, FrozenScenario] = {
        "control": source_by_id[selection.scenario_roles["control"]]
    }
    for role, threshold in (
        ("hypothesis", hypothesis_threshold),
        ("cautious", cautious_threshold),
    ):
        source = source_by_id[selection.scenario_roles[role]]
        threshold_token = format(threshold.normalize(), "f").replace(".", "p")
        quantity_policy = (
            "projected_shortage_capped_forecast_guard_no_economic_cap"
            if cap_to_projected_shortage
            else "protected_p90_forecast_and_shortage_guard_no_economic_cap"
        )
        if single_open_lot:
            quantity_policy += "_single_open_lot"
        scenario_suffix = (
            f"_forecastguard_shortage{threshold_token}_shortagecap"
            if cap_to_projected_shortage
            else f"_forecastguard_shortage{threshold_token}"
        )
        if single_open_lot:
            scenario_suffix += "_singleopenlot"
        guarded_by_role[role] = replace(
            source,
            scenario_id=f"{source.scenario_id}{scenario_suffix}",
            grow_acceleration_quantity_policy=quantity_policy,
            grow_acceleration_require_forecast_growth=True,
            grow_acceleration_min_shortage_qty=threshold,
            grow_acceleration_cap_to_projected_shortage=cap_to_projected_shortage,
            grow_acceleration_single_open_lot=single_open_lot,
        )
    scenario_roles = {
        role: guarded_by_role[role].scenario_id for role in ("control", "hypothesis", "cautious")
    }
    return ScenarioSelection(
        run_mode=selection.run_mode,
        scenarios=tuple(guarded_by_role[role] for role in ("control", "hypothesis", "cautious")),
        base_scenario_id=scenario_roles["hypothesis"],
        control_scenario_id=scenario_roles["control"],
        scenario_roles=scenario_roles,
    )


@dataclass(frozen=True)
class AccelerationSegmentRule:
    profile: str
    allowed_demand_patterns: tuple[str, ...]
    max_unit_cost_rub: Decimal
    allowed_lead_confidences: tuple[str, ...]
    max_p75_days: int


def acceleration_segment_rule(profile: str) -> AccelerationSegmentRule:
    normalized = _clean(profile).lower() or ACCELERATION_SEGMENT_PROFILE_OFF
    common = {
        "max_unit_cost_rub": Decimal("500"),
        "allowed_lead_confidences": ("high",),
        "max_p75_days": 90,
    }
    if normalized == ACCELERATION_SEGMENT_PROFILE_OFF:
        return AccelerationSegmentRule(normalized, (), ZERO, (), 0)
    if normalized == ACCELERATION_SEGMENT_PROFILE_LOW_RISK:
        return AccelerationSegmentRule(normalized, (), **common)
    if normalized == ACCELERATION_SEGMENT_PROFILE_LOW_RISK_SPARSE:
        return AccelerationSegmentRule(
            normalized,
            ("intermittent", "lumpy"),
            **common,
        )
    if normalized == ACCELERATION_SEGMENT_PROFILE_LOW_RISK_COLD_START:
        return AccelerationSegmentRule(
            normalized,
            ("no_history", "insufficient_history"),
            **common,
        )
    raise ValueError(f"unsupported acceleration segment profile: {profile}")


def apply_quick_acceleration_segment_gates(
    selection: ScenarioSelection,
    *,
    hypothesis_profile: str,
    cautious_profile: str,
) -> ScenarioSelection:
    if selection.run_mode != RUN_MODE_QUICK:
        raise ValueError("acceleration segment gates are supported only in quick mode")
    source_by_id = {scenario.scenario_id: scenario for scenario in selection.scenarios}
    gated_by_role: dict[str, FrozenScenario] = {
        "control": source_by_id[selection.scenario_roles["control"]]
    }
    for role, profile in (
        ("hypothesis", hypothesis_profile),
        ("cautious", cautious_profile),
    ):
        source = source_by_id[selection.scenario_roles[role]]
        rule = acceleration_segment_rule(profile)
        if rule.profile != ACCELERATION_SEGMENT_PROFILE_OFF and not (
            source.grow_acceleration_single_open_lot
        ):
            raise ValueError("acceleration segment gate requires single open acceleration lot")
        if rule.profile == ACCELERATION_SEGMENT_PROFILE_OFF:
            gated_by_role[role] = source
            continue
        gated_by_role[role] = replace(
            source,
            scenario_id=f"{source.scenario_id}_segment_{rule.profile}",
            grow_acceleration_quantity_policy=(
                f"{source.grow_acceleration_quantity_policy}_segment_{rule.profile}"
            ),
            grow_acceleration_segment_profile=rule.profile,
            grow_acceleration_allowed_demand_patterns=rule.allowed_demand_patterns,
            grow_acceleration_max_unit_cost_rub=rule.max_unit_cost_rub,
            grow_acceleration_allowed_lead_confidences=(rule.allowed_lead_confidences),
            grow_acceleration_max_p75_days=rule.max_p75_days,
        )
    scenario_roles = {
        role: gated_by_role[role].scenario_id for role in ("control", "hypothesis", "cautious")
    }
    return ScenarioSelection(
        run_mode=selection.run_mode,
        scenarios=tuple(gated_by_role[role] for role in ("control", "hypothesis", "cautious")),
        base_scenario_id=scenario_roles["hypothesis"],
        control_scenario_id=scenario_roles["control"],
        scenario_roles=scenario_roles,
    )


def base_pipeline_profile_fractions(profile: str) -> tuple[Decimal, Decimal, Decimal]:
    normalized = _clean(profile).lower() or BASE_PIPELINE_PROFILE_OFF
    if normalized == BASE_PIPELINE_PROFILE_OFF:
        return ONE, ONE, ONE
    if normalized == BASE_PIPELINE_PROFILE_MEDIUM_95:
        return ONE, Decimal("0.95"), Decimal("0.75")
    if normalized == BASE_PIPELINE_PROFILE_MEDIUM_90:
        return ONE, Decimal("0.90"), Decimal("0.50")
    if normalized in {
        BASE_PIPELINE_PROFILE_MEDIUM_95_MARGIN_COST_050,
        BASE_PIPELINE_PROFILE_MEDIUM_95_MARGIN_COST_100,
        BASE_PIPELINE_PROFILE_MEDIUM_95_MARGIN_COST_050_LOT_RISK_P50,
        BASE_PIPELINE_PROFILE_MEDIUM_95_MARGIN_COST_050_LOT_RISK_P75,
    }:
        return ONE, Decimal("0.95"), ONE
    raise ValueError(f"unsupported base pipeline profile: {profile}")


def base_pipeline_margin_cost_ratio_floor(profile: str) -> Decimal:
    normalized = _clean(profile).lower() or BASE_PIPELINE_PROFILE_OFF
    if normalized in {
        BASE_PIPELINE_PROFILE_MEDIUM_95_MARGIN_COST_050,
        BASE_PIPELINE_PROFILE_MEDIUM_95_MARGIN_COST_050_LOT_RISK_P50,
        BASE_PIPELINE_PROFILE_MEDIUM_95_MARGIN_COST_050_LOT_RISK_P75,
    }:
        return Decimal("0.5")
    if normalized == BASE_PIPELINE_PROFILE_MEDIUM_95_MARGIN_COST_100:
        return ONE
    if normalized in {
        BASE_PIPELINE_PROFILE_OFF,
        BASE_PIPELINE_PROFILE_MEDIUM_95,
        BASE_PIPELINE_PROFILE_MEDIUM_90,
    }:
        return ZERO
    raise ValueError(f"unsupported base pipeline profile: {profile}")


def base_pipeline_lot_risk_parameters(profile: str) -> tuple[str, Decimal]:
    normalized = _clean(profile).lower() or BASE_PIPELINE_PROFILE_OFF
    if normalized == BASE_PIPELINE_PROFILE_MEDIUM_95_MARGIN_COST_050_LOT_RISK_P50:
        return "p50", Decimal("0.90")
    if normalized == BASE_PIPELINE_PROFILE_MEDIUM_95_MARGIN_COST_050_LOT_RISK_P75:
        return "p75", Decimal("0.90")
    if normalized in {
        BASE_PIPELINE_PROFILE_OFF,
        BASE_PIPELINE_PROFILE_MEDIUM_95,
        BASE_PIPELINE_PROFILE_MEDIUM_90,
        BASE_PIPELINE_PROFILE_MEDIUM_95_MARGIN_COST_050,
        BASE_PIPELINE_PROFILE_MEDIUM_95_MARGIN_COST_100,
    }:
        return "", ONE
    raise ValueError(f"unsupported base pipeline profile: {profile}")


def risk_adjusted_base_pipeline_quantity(
    *,
    total_pipeline_qty: Decimal,
    base_fraction: Decimal,
    arrivals: Mapping[date, Mapping[str, Decimal]],
    code: str,
    as_of: date,
    boundary_days: int,
    risk_fraction: Decimal,
) -> tuple[Decimal, Decimal]:
    total = max(ZERO, _decimal(total_pipeline_qty))
    normal_share = max(ZERO, min(ONE, _decimal(base_fraction)))
    risky_share = max(ZERO, min(normal_share, _decimal(risk_fraction)))
    if total <= ZERO or boundary_days <= 0 or risky_share >= normal_share:
        return total * normal_share, ZERO
    risky_qty = min(
        total,
        sum(
            (
                max(ZERO, _decimal(rows.get(code)))
                for arrival_at, rows in arrivals.items()
                if arrival_at > as_of and (arrival_at - as_of).days > boundary_days
            ),
            ZERO,
        ),
    )
    effective = (total - risky_qty) * normal_share + risky_qty * risky_share
    return effective, risky_qty


def base_pipeline_fraction(
    confidence: str,
    *,
    high_fraction: Decimal,
    medium_fraction: Decimal,
    low_fraction: Decimal,
    min_margin_to_cost_ratio: Decimal = ZERO,
    gross_margin_per_unit_rub: Decimal = ZERO,
    inventory_cost_per_unit_rub: Decimal = ZERO,
) -> Decimal:
    normalized = _clean(confidence).lower()
    if normalized == "high":
        return max(ZERO, min(ONE, _decimal(high_fraction)))
    if normalized == "medium":
        ratio_floor = max(ZERO, _decimal(min_margin_to_cost_ratio))
        unit_cost = _decimal(inventory_cost_per_unit_rub)
        gross_margin = _decimal(gross_margin_per_unit_rub)
        if ratio_floor > ZERO and (unit_cost <= ZERO or gross_margin / unit_cost < ratio_floor):
            return ONE
        return max(ZERO, min(ONE, _decimal(medium_fraction)))
    return max(ZERO, min(ONE, _decimal(low_fraction)))


def apply_quick_base_pipeline_profiles(
    selection: ScenarioSelection,
    *,
    hypothesis_profile: str,
    cautious_profile: str,
) -> ScenarioSelection:
    if selection.run_mode != RUN_MODE_QUICK:
        raise ValueError("base pipeline profiles are supported only in quick mode")
    source_by_id = {scenario.scenario_id: scenario for scenario in selection.scenarios}
    control = source_by_id[selection.scenario_roles["control"]]
    if control.grow_acceleration_profile != "off":
        raise ValueError("base pipeline challenger requires an acceleration-off control")
    scenarios_by_role = {"control": control}
    for role, profile in (
        ("hypothesis", hypothesis_profile),
        ("cautious", cautious_profile),
    ):
        normalized = _clean(profile).lower()
        high, medium, low = base_pipeline_profile_fractions(normalized)
        margin_cost_ratio_floor = base_pipeline_margin_cost_ratio_floor(normalized)
        lot_risk_boundary, lot_risk_fraction = base_pipeline_lot_risk_parameters(normalized)
        if normalized == BASE_PIPELINE_PROFILE_OFF:
            raise ValueError("base pipeline challenger profiles must not be off")
        scenarios_by_role[role] = replace(
            control,
            scenario_id=f"{control.scenario_id}_basepipeline_{normalized}",
            base_pipeline_profile=normalized,
            base_pipeline_high_fraction=high,
            base_pipeline_medium_fraction=medium,
            base_pipeline_low_fraction=low,
            base_pipeline_min_margin_to_cost_ratio=margin_cost_ratio_floor,
            base_pipeline_lot_risk_boundary=lot_risk_boundary,
            base_pipeline_lot_risk_fraction=lot_risk_fraction,
        )
    if scenarios_by_role["hypothesis"].scenario_id == scenarios_by_role["cautious"].scenario_id:
        raise ValueError("base pipeline hypothesis and cautious profiles must differ")
    scenario_roles = {
        role: scenarios_by_role[role].scenario_id for role in ("control", "hypothesis", "cautious")
    }
    return ScenarioSelection(
        run_mode=selection.run_mode,
        scenarios=tuple(scenarios_by_role[role] for role in ("control", "hypothesis", "cautious")),
        base_scenario_id=scenario_roles["hypothesis"],
        control_scenario_id=scenario_roles["control"],
        scenario_roles=scenario_roles,
    )


@dataclass
class GrowProtectionState:
    entered_at: date | None
    min_qty: Decimal
    max_qty: Decimal


@dataclass(frozen=True)
class ServiceFloorCandidate:
    code: str
    requested_units: Decimal
    unit_cost_rub: Decimal
    gross_margin_per_unit_rub: Decimal
    error_samples: tuple[Decimal, ...]


@dataclass(frozen=True)
class DemandAccelerationSignal:
    recent_qty: Decimal
    baseline_qty: Decimal
    recent_rate: Decimal
    baseline_rate: Decimal
    rate_ratio: Decimal
    triggered: bool


@dataclass(frozen=True)
class AccelerationShortageGuard:
    forecast_growth_passed: bool
    projected_demand_qty: Decimal
    inventory_position_qty: Decimal
    projected_shortage_qty: Decimal
    shortage_passed: bool
    eligible: bool
    gross_projected_shortage_qty: Decimal = ZERO
    open_acceleration_protection_qty: Decimal = ZERO


@dataclass(frozen=True)
class AccelerationSegmentGate:
    demand_pattern: str
    pattern_passed: bool
    cost_passed: bool
    confidence_passed: bool
    p75_passed: bool
    eligible: bool


def evaluate_acceleration_segment_gate(
    *,
    demand_pattern: str,
    unit_cost_rub: Decimal,
    lead_time_confidence: str,
    lead_time_p75_days: int,
    allowed_demand_patterns: Sequence[str],
    max_unit_cost_rub: Decimal,
    allowed_lead_confidences: Sequence[str],
    max_p75_days: int,
) -> AccelerationSegmentGate:
    normalized_pattern = _clean(demand_pattern).lower() or "no_history"
    normalized_patterns = {
        _clean(value).lower() for value in allowed_demand_patterns if _clean(value)
    }
    normalized_confidence = _clean(lead_time_confidence).lower()
    normalized_confidences = {
        _clean(value).lower() for value in allowed_lead_confidences if _clean(value)
    }
    pattern_passed = not normalized_patterns or normalized_pattern in normalized_patterns
    cost = max(ZERO, unit_cost_rub)
    cost_passed = max_unit_cost_rub <= ZERO or (ZERO < cost < max_unit_cost_rub)
    confidence_passed = (
        not normalized_confidences or normalized_confidence in normalized_confidences
    )
    p75_passed = max_p75_days <= 0 or (0 < lead_time_p75_days <= max_p75_days)
    return AccelerationSegmentGate(
        demand_pattern=normalized_pattern,
        pattern_passed=pattern_passed,
        cost_passed=cost_passed,
        confidence_passed=confidence_passed,
        p75_passed=p75_passed,
        eligible=(pattern_passed and cost_passed and confidence_passed and p75_passed),
    )


def _load_scenarios(path: Path) -> list[FrozenScenario]:
    scenarios: list[FrozenScenario] = []
    for row in _read_csv(path):
        scenario_id = _clean(row.get("scenario_id"))
        legacy = scenario_id == "legacy"
        scenarios.append(
            FrozenScenario(
                scenario_id=scenario_id,
                stage_profile=_clean(row.get("stage_profile")),
                kmp4_weight=_decimal(row.get("kmp4_weight")),
                site_profile=_clean(row.get("site_profile")) or "off",
                site_order_weight=_decimal(row.get("site_order_weight")),
                site_unordered_cart_weight=_decimal(row.get("site_unordered_cart_weight")),
                grow_weekly_reduction_cap=_decimal(row.get("grow_weekly_reduction_cap")),
                forecast_error_percentile=_decimal(row.get("forecast_error_percentile")),
                grow_entry_protection_weeks=int(row.get("grow_entry_protection_weeks") or 0),
                grow_service_floor_percentile=_decimal(row.get("grow_service_floor_percentile")),
                grow_service_floor_sku_cap_rub=_decimal(row.get("grow_service_floor_sku_cap_rub")),
                grow_service_floor_stage_budget_rub=_decimal(
                    row.get("grow_service_floor_stage_budget_rub")
                ),
                grow_acceleration_profile=_clean(row.get("grow_acceleration_profile")) or "off",
                grow_acceleration_quantity_policy=(
                    _clean(row.get("grow_acceleration_quantity_policy")) or "off"
                ),
                grow_acceleration_recent_days=int(row.get("grow_acceleration_recent_days") or 0),
                grow_acceleration_baseline_days=int(
                    row.get("grow_acceleration_baseline_days") or 0
                ),
                grow_acceleration_min_recent_sales=_decimal(
                    row.get("grow_acceleration_min_recent_sales")
                ),
                grow_acceleration_rate_multiplier=_decimal(
                    row.get("grow_acceleration_rate_multiplier")
                ),
                grow_acceleration_sku_cap_rub=_decimal(row.get("grow_acceleration_sku_cap_rub")),
                grow_acceleration_stage_budget_rub=_decimal(
                    row.get("grow_acceleration_stage_budget_rub")
                ),
                grow_acceleration_medium_pipeline_fraction=_decimal(
                    row.get("grow_acceleration_medium_pipeline_fraction") or ONE
                ),
                grow_acceleration_low_pipeline_fraction=_decimal(
                    row.get("grow_acceleration_low_pipeline_fraction") or ONE
                ),
                grow_acceleration_require_forecast_growth=(
                    _clean(row.get("grow_acceleration_require_forecast_growth")).lower()
                    in {"1", "true", "yes", "y"}
                ),
                grow_acceleration_min_shortage_qty=_decimal(
                    row.get("grow_acceleration_min_shortage_qty")
                ),
                grow_acceleration_cap_to_projected_shortage=(
                    _clean(row.get("grow_acceleration_cap_to_projected_shortage")).lower()
                    in {"1", "true", "yes", "y"}
                ),
                grow_acceleration_single_open_lot=(
                    _clean(row.get("grow_acceleration_single_open_lot")).lower()
                    in {"1", "true", "yes", "y"}
                ),
                grow_acceleration_segment_profile=(
                    _clean(row.get("grow_acceleration_segment_profile"))
                    or ACCELERATION_SEGMENT_PROFILE_OFF
                ),
                grow_acceleration_allowed_demand_patterns=tuple(
                    value
                    for value in (
                        _clean(item).lower()
                        for item in _clean(
                            row.get("grow_acceleration_allowed_demand_patterns")
                        ).split(",")
                    )
                    if value
                ),
                grow_acceleration_max_unit_cost_rub=_decimal(
                    row.get("grow_acceleration_max_unit_cost_rub")
                ),
                grow_acceleration_allowed_lead_confidences=tuple(
                    value
                    for value in (
                        _clean(item).lower()
                        for item in _clean(
                            row.get("grow_acceleration_allowed_lead_confidences")
                        ).split(",")
                    )
                    if value
                ),
                grow_acceleration_max_p75_days=int(row.get("grow_acceleration_max_p75_days") or 0),
                base_pipeline_profile=(
                    _clean(row.get("base_pipeline_profile")) or BASE_PIPELINE_PROFILE_OFF
                ),
                base_pipeline_high_fraction=_decimal(row.get("base_pipeline_high_fraction") or ONE),
                base_pipeline_medium_fraction=_decimal(
                    row.get("base_pipeline_medium_fraction") or ONE
                ),
                base_pipeline_low_fraction=_decimal(row.get("base_pipeline_low_fraction") or ONE),
                base_pipeline_min_margin_to_cost_ratio=_decimal(
                    row.get("base_pipeline_min_margin_to_cost_ratio")
                ),
                base_pipeline_lot_risk_boundary=_clean(
                    row.get("base_pipeline_lot_risk_boundary")
                ).lower(),
                base_pipeline_lot_risk_fraction=_decimal(
                    row.get("base_pipeline_lot_risk_fraction") or ONE
                ),
                cost=CarryingCostScenario(
                    name=_clean(row.get("holding_cost_scenario")),
                    capital_annual_rate=_decimal(row.get("capital_annual_rate")),
                    storage_annual_rate=_decimal(row.get("storage_annual_rate")),
                    obsolescence_annual_rate=_decimal(row.get("obsolescence_annual_rate")),
                ),
                legacy=legacy,
            )
        )
    return scenarios


def _profile_value(row: Mapping[str, Any], profile: str, suffix: str) -> Decimal:
    return _decimal(row.get(f"launch_{profile}_{suffix}"))


def _aggregate_metric(metrics: Mapping[str, Metric]) -> Metric:
    out = Metric()
    for row in metrics.values():
        out.observed_demand_qty += row.observed_demand_qty
        out.hidden_demand_qty += row.hidden_demand_qty
        out.hidden_kmp4_qty += row.hidden_kmp4_qty
        out.hidden_site_order_qty += row.hidden_site_order_qty
        out.hidden_site_cart_qty += row.hidden_site_cart_qty
        out.hidden_reserve_backlog_qty += row.hidden_reserve_backlog_qty
        out.served_observed_qty += row.served_observed_qty
        out.served_hidden_qty += row.served_hidden_qty
        out.served_hidden_kmp4_qty += row.served_hidden_kmp4_qty
        out.served_hidden_site_order_qty += row.served_hidden_site_order_qty
        out.served_hidden_site_cart_qty += row.served_hidden_site_cart_qty
        out.served_hidden_reserve_backlog_qty += row.served_hidden_reserve_backlog_qty
        out.lost_observed_qty += row.lost_observed_qty
        out.lost_hidden_qty += row.lost_hidden_qty
        out.gross_profit_rub += row.gross_profit_rub
        out.inventory_qty_days += row.inventory_qty_days
        out.priced_inventory_qty_days += row.priced_inventory_qty_days
        out.inventory_value_days_rub += row.inventory_value_days_rub
        out.ending_inventory_qty += row.ending_inventory_qty
        out.ending_target_stock_qty += row.ending_target_stock_qty
        out.order_qty += row.order_qty
        out.order_value_rub += row.order_value_rub
        out.order_lines += row.order_lines
        out.manual_order_lines += row.manual_order_lines
        out.manual_review_created += row.manual_review_created
        out.manual_review_updated += row.manual_review_updated
        out.safety_stock_units_ordered += row.safety_stock_units_ordered
        out.exogenous_launch_seed_qty += row.exogenous_launch_seed_qty
    return out


def _summary(
    *,
    scenario: FrozenScenario,
    strategy: str,
    metrics: Mapping[str, Metric],
    period_days: int,
) -> dict[str, Any]:
    total = _aggregate_metric(metrics)
    ending_excess_stock_qty = sum(
        (
            max(ZERO, row.ending_inventory_qty - row.ending_target_stock_qty)
            for row in metrics.values()
        ),
        ZERO,
    )
    potential = total.potential_demand_qty
    average_inventory = total.inventory_value_days_rub / Decimal(period_days)
    carrying_cost = (
        average_inventory * scenario.cost.total_annual_rate * Decimal(period_days) / YEAR_DAYS
    )
    gmroi = (
        total.gross_profit_rub * YEAR_DAYS / Decimal(period_days) / average_inventory
        if average_inventory > ZERO
        else ZERO
    )
    return {
        "scenario_id": scenario.scenario_id,
        "strategy": strategy,
        "stage_profile": scenario.stage_profile,
        "kmp4_weight": str(scenario.kmp4_weight),
        "site_profile": scenario.site_profile,
        "site_order_weight": str(scenario.site_order_weight),
        "site_unordered_cart_weight": str(scenario.site_unordered_cart_weight),
        "grow_weekly_reduction_cap": str(scenario.grow_weekly_reduction_cap),
        "forecast_error_percentile": str(scenario.forecast_error_percentile),
        "grow_entry_protection_weeks": scenario.grow_entry_protection_weeks,
        "grow_service_floor_percentile": str(scenario.grow_service_floor_percentile),
        "grow_service_floor_sku_cap_rub": str(scenario.grow_service_floor_sku_cap_rub),
        "grow_service_floor_stage_budget_rub": str(scenario.grow_service_floor_stage_budget_rub),
        "grow_acceleration_profile": scenario.grow_acceleration_profile,
        "grow_acceleration_quantity_policy": scenario.grow_acceleration_quantity_policy,
        "grow_acceleration_recent_days": scenario.grow_acceleration_recent_days,
        "grow_acceleration_baseline_days": scenario.grow_acceleration_baseline_days,
        "grow_acceleration_min_recent_sales": str(scenario.grow_acceleration_min_recent_sales),
        "grow_acceleration_rate_multiplier": str(scenario.grow_acceleration_rate_multiplier),
        "grow_acceleration_sku_cap_rub": str(scenario.grow_acceleration_sku_cap_rub),
        "grow_acceleration_stage_budget_rub": str(scenario.grow_acceleration_stage_budget_rub),
        "grow_acceleration_medium_pipeline_fraction": str(
            scenario.grow_acceleration_medium_pipeline_fraction
        ),
        "grow_acceleration_low_pipeline_fraction": str(
            scenario.grow_acceleration_low_pipeline_fraction
        ),
        "grow_acceleration_require_forecast_growth": int(
            scenario.grow_acceleration_require_forecast_growth
        ),
        "grow_acceleration_min_shortage_qty": str(scenario.grow_acceleration_min_shortage_qty),
        "grow_acceleration_cap_to_projected_shortage": int(
            scenario.grow_acceleration_cap_to_projected_shortage
        ),
        "grow_acceleration_single_open_lot": int(scenario.grow_acceleration_single_open_lot),
        "grow_acceleration_segment_profile": scenario.grow_acceleration_segment_profile,
        "grow_acceleration_allowed_demand_patterns": ",".join(
            scenario.grow_acceleration_allowed_demand_patterns
        ),
        "grow_acceleration_max_unit_cost_rub": str(scenario.grow_acceleration_max_unit_cost_rub),
        "grow_acceleration_allowed_lead_confidences": ",".join(
            scenario.grow_acceleration_allowed_lead_confidences
        ),
        "grow_acceleration_max_p75_days": scenario.grow_acceleration_max_p75_days,
        "base_pipeline_profile": scenario.base_pipeline_profile,
        "base_pipeline_high_fraction": str(scenario.base_pipeline_high_fraction),
        "base_pipeline_medium_fraction": str(scenario.base_pipeline_medium_fraction),
        "base_pipeline_low_fraction": str(scenario.base_pipeline_low_fraction),
        "base_pipeline_min_margin_to_cost_ratio": str(
            scenario.base_pipeline_min_margin_to_cost_ratio
        ),
        "base_pipeline_lot_risk_boundary": scenario.base_pipeline_lot_risk_boundary,
        "base_pipeline_lot_risk_fraction": str(scenario.base_pipeline_lot_risk_fraction),
        "holding_cost_scenario": scenario.cost.name,
        "capital_annual_rate": str(scenario.cost.capital_annual_rate),
        "storage_annual_rate": str(scenario.cost.storage_annual_rate),
        "obsolescence_annual_rate": str(scenario.cost.obsolescence_annual_rate),
        "potential_demand_qty": str(potential),
        "observed_demand_qty": str(total.observed_demand_qty),
        "hidden_demand_qty": str(total.hidden_demand_qty),
        "hidden_kmp4_qty": str(total.hidden_kmp4_qty),
        "hidden_site_order_qty": str(total.hidden_site_order_qty),
        "hidden_site_cart_qty": str(total.hidden_site_cart_qty),
        "hidden_reserve_backlog_qty": str(total.hidden_reserve_backlog_qty),
        "served_qty": str(total.served_qty),
        "served_observed_qty": str(total.served_observed_qty),
        "served_hidden_qty": str(total.served_hidden_qty),
        "served_hidden_kmp4_qty": str(total.served_hidden_kmp4_qty),
        "served_hidden_site_order_qty": str(total.served_hidden_site_order_qty),
        "served_hidden_site_cart_qty": str(total.served_hidden_site_cart_qty),
        "served_hidden_reserve_backlog_qty": str(total.served_hidden_reserve_backlog_qty),
        "lost_qty": str(total.lost_qty),
        "lost_observed_qty": str(total.lost_observed_qty),
        "lost_hidden_qty": str(total.lost_hidden_qty),
        "fill_rate": str(total.served_qty / potential if potential > ZERO else ONE),
        "observed_fill_rate": str(
            total.served_observed_qty / total.observed_demand_qty
            if total.observed_demand_qty > ZERO
            else ONE
        ),
        "hidden_fill_rate": str(
            total.served_hidden_qty / total.hidden_demand_qty
            if total.hidden_demand_qty > ZERO
            else ONE
        ),
        "gross_profit_rub": str(total.gross_profit_rub),
        "average_inventory_value_rub": str(average_inventory),
        "carrying_cost_rub": str(carrying_cost),
        "economic_contribution_rub": str(total.gross_profit_rub - carrying_cost),
        "gmroi_annualized": str(gmroi),
        "ending_inventory_qty": str(total.ending_inventory_qty),
        "ending_target_stock_qty": str(total.ending_target_stock_qty),
        "ending_excess_stock_qty": str(ending_excess_stock_qty),
        "order_qty": str(total.order_qty),
        "order_value_rub": str(total.order_value_rub),
        "order_lines": total.order_lines,
        "manual_order_lines": total.manual_order_lines,
        "manual_review_created": total.manual_review_created,
        "manual_review_updated": total.manual_review_updated,
        "safety_stock_units_ordered": str(total.safety_stock_units_ordered),
        "exogenous_launch_seed_qty": str(total.exogenous_launch_seed_qty),
        "inventory_valuation_coverage": str(
            total.priced_inventory_qty_days / total.inventory_qty_days
            if total.inventory_qty_days > ZERO
            else ONE
        ),
    }


def _acceptance_result(
    actual: Mapping[str, Any],
    model: Mapping[str, Any],
) -> dict[str, bool]:
    gross_profit_pass = _decimal(model["gross_profit_rub"]) + Decimal("1") >= _decimal(
        actual["gross_profit_rub"]
    )
    fill_rate_pass = _decimal(model["fill_rate"]) + Decimal("0.0001") >= _decimal(
        actual["fill_rate"]
    )
    capital_or_gmroi_pass = _decimal(model["average_inventory_value_rub"]) <= _decimal(
        actual["average_inventory_value_rub"]
    ) or _decimal(model["gmroi_annualized"]) >= _decimal(actual["gmroi_annualized"])
    return {
        "gross_profit_not_lower": gross_profit_pass,
        "fill_rate_not_lower": fill_rate_pass,
        "capital_lower_or_gmroi_higher": capital_or_gmroi_pass,
        "passed": gross_profit_pass and fill_rate_pass and capital_or_gmroi_pass,
    }


def _free_initial_pipeline(
    rows: Sequence[Mapping[str, Any]],
    *,
    placed_by_code: Mapping[str, Decimal],
    date_from: date,
) -> dict[str, list[tuple[date, Decimal]]]:
    by_code: dict[str, list[tuple[date, Decimal]]] = defaultdict(list)
    for row in rows:
        code = _clean(row.get("nomenclature_code"))
        arrival = _date(row.get("arrival_at")) or date_from
        qty = max(ZERO, _decimal(row.get("quantity")))
        if code and qty > ZERO:
            by_code[code].append((max(date_from, arrival), qty))
    for code, lots in by_code.items():
        allocated = max(ZERO, _decimal(placed_by_code.get(code)))
        free: list[tuple[date, Decimal]] = []
        for arrival, qty in sorted(lots):
            used = min(qty, allocated)
            allocated -= used
            if qty - used > ZERO:
                free.append((arrival, qty - used))
        by_code[code] = free
    return dict(by_code)


def historical_forecast_error_samples(
    decision_rows: Sequence[Mapping[str, Any]],
    sales: Mapping[date, Decimal],
    *,
    as_of: date,
    order_cadence_days: int,
    lookback_days: int,
) -> list[Decimal]:
    """Return only fully observed past underforecast errors at decision grain."""

    earliest = as_of - timedelta(days=max(1, lookback_days))
    samples: list[Decimal] = []
    for row in decision_rows:
        if _clean(row.get("scheduled_review")) != "1":
            continue
        decision_date = _date(row.get("decision_date"))
        if decision_date is None or decision_date < earliest or decision_date >= as_of:
            continue
        lead_days = max(1, int(row.get("lead_time_p50_days") or 52))
        horizon_days = lead_days + max(1, order_cadence_days)
        observed_through = decision_date + timedelta(days=horizon_days)
        if observed_through >= as_of:
            continue
        actual_demand = sum(
            (
                max(ZERO, _decimal(qty))
                for business_date, qty in sales.items()
                if decision_date < business_date <= observed_through
            ),
            ZERO,
        )
        predicted_demand = max(
            ZERO,
            _decimal(row.get("forecast_rate_sales")) * Decimal(horizon_days),
        )
        samples.append(max(ZERO, actual_demand - predicted_demand))
    return samples


def empirical_underforecast_percentile(
    samples: Sequence[Decimal],
    *,
    percentile: Decimal,
    min_samples: int,
) -> Decimal:
    """Return a nearest-rank percentile using only the supplied completed windows."""

    cleaned = sorted(max(ZERO, _decimal(value)) for value in samples)
    if len(cleaned) < min_samples or percentile <= ZERO or percentile >= ONE:
        return ZERO
    rank = int(_ceil(percentile * Decimal(len(cleaned))))
    return _ceil(cleaned[max(0, rank - 1)])


def calculate_demand_acceleration(
    sales_by_day: Mapping[date, Decimal],
    *,
    as_of: date,
    recent_days: int,
    baseline_days: int,
    min_recent_sales: Decimal,
    rate_multiplier: Decimal,
) -> DemandAccelerationSignal:
    """Detect acceleration from completed past days only."""

    if recent_days <= 0 or baseline_days <= 0 or min_recent_sales <= ZERO or rate_multiplier <= ONE:
        return DemandAccelerationSignal(ZERO, ZERO, ZERO, ZERO, ZERO, False)
    recent_start = as_of - timedelta(days=recent_days)
    baseline_start = recent_start - timedelta(days=baseline_days)
    recent_qty = sum(
        (
            max(ZERO, _decimal(quantity))
            for business_date, quantity in sales_by_day.items()
            if recent_start <= business_date < as_of
        ),
        ZERO,
    )
    baseline_qty = sum(
        (
            max(ZERO, _decimal(quantity))
            for business_date, quantity in sales_by_day.items()
            if baseline_start <= business_date < recent_start
        ),
        ZERO,
    )
    recent_rate = recent_qty / Decimal(recent_days)
    baseline_rate = baseline_qty / Decimal(baseline_days)
    rate_ratio = (
        recent_rate / baseline_rate
        if baseline_rate > ZERO
        else Decimal("999") if recent_rate > ZERO else ZERO
    )
    triggered = bool(
        recent_qty >= min_recent_sales
        and (baseline_rate <= ZERO or recent_rate >= baseline_rate * rate_multiplier)
    )
    return DemandAccelerationSignal(
        recent_qty=recent_qty,
        baseline_qty=baseline_qty,
        recent_rate=recent_rate,
        baseline_rate=baseline_rate,
        rate_ratio=rate_ratio,
        triggered=triggered,
    )


def evaluate_acceleration_shortage_guard(
    *,
    signal: DemandAccelerationSignal,
    forecast_rate: Decimal,
    lead_time_days: int,
    model_stock_qty: Decimal,
    effective_reserve_qty: Decimal,
    effective_pipeline_qty: Decimal,
    open_acceleration_protection_qty: Decimal = ZERO,
    require_forecast_growth: bool,
    min_shortage_qty: Decimal,
) -> AccelerationShortageGuard:
    projected_demand = _ceil(max(ZERO, signal.recent_rate) * Decimal(max(1, lead_time_days)))
    gross_inventory_position = (
        max(ZERO, model_stock_qty)
        - max(ZERO, effective_reserve_qty)
        + max(ZERO, effective_pipeline_qty)
    )
    open_protection = max(ZERO, open_acceleration_protection_qty)
    inventory_position = gross_inventory_position + open_protection
    gross_projected_shortage = _ceil(max(ZERO, projected_demand - gross_inventory_position))
    projected_shortage = _ceil(max(ZERO, gross_projected_shortage - open_protection))
    forecast_growth_passed = bool(
        not require_forecast_growth or signal.recent_rate > max(ZERO, forecast_rate)
    )
    shortage_threshold = max(ZERO, min_shortage_qty)
    shortage_passed = bool(shortage_threshold <= ZERO or projected_shortage >= shortage_threshold)
    return AccelerationShortageGuard(
        forecast_growth_passed=forecast_growth_passed,
        projected_demand_qty=projected_demand,
        inventory_position_qty=inventory_position,
        projected_shortage_qty=projected_shortage,
        shortage_passed=shortage_passed,
        eligible=signal.triggered and forecast_growth_passed and shortage_passed,
        gross_projected_shortage_qty=gross_projected_shortage,
        open_acceleration_protection_qty=open_protection,
    )


def release_open_acceleration_protection(
    open_qty: Decimal,
    *,
    arrived_qty: Decimal = ZERO,
    cancelled_qty: Decimal = ZERO,
) -> Decimal:
    """Release only already-open acceleration protection, never future lots."""

    released = max(ZERO, arrived_qty) + max(ZERO, cancelled_qty)
    return max(ZERO, max(ZERO, open_qty) - released)


def completed_hybrid_demand_rate(
    sales_by_day: Mapping[date, Decimal],
    *,
    as_of: date,
    forecast_rate: Decimal,
    window_days: Sequence[int] = (30, 90, 180),
) -> Decimal:
    """Use only completed days before ``as_of`` and never lower the frozen forecast."""

    completed_rates: list[Decimal] = []
    for source_days in window_days:
        days = max(1, int(source_days))
        window_from = as_of - timedelta(days=days)
        completed_qty = sum(
            (
                max(ZERO, _decimal(quantity))
                for business_date, quantity in sales_by_day.items()
                if window_from <= business_date < as_of
            ),
            ZERO,
        )
        completed_rates.append(completed_qty / Decimal(days))
    return max((max(ZERO, forecast_rate), *completed_rates))


def evaluate_hybrid_coverable_gap(
    *,
    as_of: date,
    demand_rate: Decimal,
    new_arrival_lead_days: int,
    model_stock_qty: Decimal,
    effective_reserve_qty: Decimal,
    arrivals: Mapping[date, Mapping[str, Decimal]],
    code: str,
    open_hybrid_qty: Decimal = ZERO,
    min_coverable_days: int = 0,
) -> HybridGapEvaluation:
    """Calculate only the shortage a new lot can physically cover before an open lot."""

    minimum_days = int(min_coverable_days)
    if minimum_days < 0:
        raise ValueError("hybrid gap minimum coverable days cannot be negative")
    rate = max(ZERO, demand_rate)
    new_arrival_date = as_of + timedelta(days=max(1, int(new_arrival_lead_days)))
    reliable_dates = sorted(
        arrival_date
        for arrival_date, quantities in arrivals.items()
        if arrival_date > new_arrival_date and quantities.get(code, ZERO) > ZERO
    )
    reliable_arrival_date = reliable_dates[0] if reliable_dates else None
    open_qty = max(ZERO, open_hybrid_qty)
    free_stock = max(ZERO, model_stock_qty - max(ZERO, effective_reserve_qty))
    if reliable_arrival_date is None or rate <= ZERO:
        return HybridGapEvaluation(
            demand_rate=rate,
            new_arrival_date=new_arrival_date,
            reliable_arrival_date=reliable_arrival_date,
            coverable_days=0,
            stock_at_new_arrival_qty=free_stock,
            coverable_demand_qty=ZERO,
            coverable_shortage_qty=ZERO,
            open_hybrid_qty=open_qty,
            eligible=False,
        )

    reliable_before_new = sum(
        (
            max(ZERO, quantities.get(code, ZERO))
            for arrival_date, quantities in arrivals.items()
            if as_of < arrival_date <= new_arrival_date
        ),
        ZERO,
    )
    demand_before_new = rate * Decimal((new_arrival_date - as_of).days)
    stock_at_new = max(ZERO, free_stock + reliable_before_new - demand_before_new)
    coverable_days = max(0, (reliable_arrival_date - new_arrival_date).days)
    coverable_demand = rate * Decimal(coverable_days)
    shortage = _ceil(max(ZERO, coverable_demand - stock_at_new))
    return HybridGapEvaluation(
        demand_rate=rate,
        new_arrival_date=new_arrival_date,
        reliable_arrival_date=reliable_arrival_date,
        coverable_days=coverable_days,
        stock_at_new_arrival_qty=stock_at_new,
        coverable_demand_qty=coverable_demand,
        coverable_shortage_qty=shortage,
        open_hybrid_qty=open_qty,
        eligible=bool(shortage > ZERO and open_qty <= ZERO and coverable_days >= minimum_days),
    )


def cap_acceleration_to_projected_shortage(
    requested_units: Decimal,
    *,
    projected_shortage_qty: Decimal,
    enabled: bool,
) -> Decimal:
    requested = max(ZERO, _ceil(requested_units))
    if not enabled:
        return requested
    return min(requested, max(ZERO, _ceil(projected_shortage_qty)))


def acceleration_incremental_units(
    *,
    signal: DemandAccelerationSignal,
    forecast_rate: Decimal,
    coverage_days: int,
    percentile_safety_units: Decimal,
    ordinary_safety_units: Decimal,
    max_units: int,
) -> Decimal:
    """Protect P90 and acceleration demand without a per-SKU economic quantity cap."""

    if not signal.triggered or coverage_days <= 0:
        return ZERO
    acceleration_gap = _ceil(
        max(ZERO, signal.recent_rate - max(ZERO, forecast_rate)) * Decimal(coverage_days)
    )
    protected_target = min(
        Decimal(max_units),
        max(max(ZERO, percentile_safety_units), acceleration_gap),
    )
    ordinary_safety = min(Decimal(max_units), max(ZERO, ordinary_safety_units))
    return max(ZERO, protected_target - ordinary_safety)


def acceleration_pipeline_fraction(
    confidence: str,
    *,
    medium_fraction: Decimal,
    low_fraction: Decimal,
) -> Decimal:
    normalized = _clean(confidence).lower()
    if normalized == "high":
        return ONE
    if normalized == "medium":
        return min(ONE, max(ZERO, medium_fraction))
    return min(ONE, max(ZERO, low_fraction))


def apply_service_floor_sku_cap(
    requested_units: Decimal,
    *,
    unit_cost_rub: Decimal,
    per_sku_cap_rub: Decimal,
) -> Decimal:
    requested = max(ZERO, _ceil(requested_units))
    if per_sku_cap_rub <= ZERO:
        return requested
    if unit_cost_rub <= ZERO:
        return ZERO
    affordable = (per_sku_cap_rub / unit_cost_rub).to_integral_value(rounding=ROUND_FLOOR)
    return min(requested, max(ZERO, affordable))


def combine_service_floor_with_economic_stock(
    *,
    service_floor_units: Decimal,
    economic_cap_units: Decimal,
    economic_percentile_target_units: Decimal,
) -> Decimal:
    return max(
        ZERO,
        service_floor_units,
        min(economic_cap_units, economic_percentile_target_units),
    )


def allocate_service_floor_budget(
    candidates: Sequence[ServiceFloorCandidate],
    *,
    stage_budget_rub: Decimal,
) -> dict[str, Decimal]:
    """Allocate a concurrent stage budget by marginal saved-margin return."""

    allocated = {candidate.code: ZERO for candidate in candidates}
    if stage_budget_rub <= ZERO:
        return {
            candidate.code: max(ZERO, _ceil(candidate.requested_units)) for candidate in candidates
        }
    ranked_units: list[tuple[Decimal, Decimal, str, int, Decimal]] = []
    for candidate in candidates:
        unit_cost = max(ZERO, candidate.unit_cost_rub)
        if unit_cost <= ZERO:
            continue
        samples = tuple(max(ZERO, value) for value in candidate.error_samples)
        sample_count = Decimal(len(samples))
        for unit_number in range(1, int(max(ZERO, candidate.requested_units)) + 1):
            probability = (
                Decimal(sum(sample > Decimal(unit_number - 1) for sample in samples)) / sample_count
                if sample_count > ZERO
                else ZERO
            )
            expected_saved_margin = max(ZERO, candidate.gross_margin_per_unit_rub) * probability
            return_per_ruble = expected_saved_margin / unit_cost
            ranked_units.append(
                (return_per_ruble, probability, candidate.code, unit_number, unit_cost)
            )
    ranked_units.sort(key=lambda row: (-row[0], -row[1], row[2], row[3]))
    remaining = max(ZERO, stage_budget_rub)
    for _, _, code, _, unit_cost in ranked_units:
        if unit_cost <= remaining:
            allocated[code] += ONE
            remaining -= unit_cost
    return allocated


def apply_grow_target_protection(
    *,
    raw_min_qty: Decimal,
    raw_max_qty: Decimal,
    as_of: date,
    scheduled_review: bool,
    entered_today: bool,
    weekly_reduction_cap: Decimal,
    entry_protection_weeks: int,
    state: GrowProtectionState | None,
) -> tuple[Decimal, Decimal, GrowProtectionState, str]:
    """Protect grow-stage min/max from look-ahead-free abrupt target reductions."""

    raw_min = max(ZERO, _ceil(raw_min_qty))
    raw_max = max(raw_min, _ceil(raw_max_qty))
    if state is None or entered_today:
        state = GrowProtectionState(
            entered_at=as_of if entered_today else None,
            min_qty=raw_min,
            max_qty=raw_max,
        )
        return raw_min, raw_max, state, "entry_hold" if entered_today else "initialized"

    entry_hold_active = (
        state.entered_at is not None
        and entry_protection_weeks > 0
        and as_of < state.entered_at + timedelta(weeks=entry_protection_weeks)
    )
    if entry_hold_active:
        protected_min = max(raw_min, state.min_qty)
        protected_max = max(raw_max, state.max_qty, protected_min)
        reason = "entry_hold" if protected_min > raw_min or protected_max > raw_max else "none"
    elif scheduled_review and weekly_reduction_cap > ZERO:
        reduction_factor = ONE - weekly_reduction_cap
        protected_min = max(raw_min, _ceil(state.min_qty * reduction_factor))
        protected_max = max(
            raw_max,
            _ceil(state.max_qty * reduction_factor),
            protected_min,
        )
        reason = (
            "weekly_reduction_cap" if protected_min > raw_min or protected_max > raw_max else "none"
        )
    else:
        protected_min = max(raw_min, state.min_qty)
        protected_max = max(raw_max, state.max_qty, protected_min)
        reason = (
            "between_reviews_floor"
            if protected_min > raw_min or protected_max > raw_max
            else "none"
        )
    state.min_qty = protected_min
    state.max_qty = protected_max
    return protected_min, protected_max, state, reason


def _allocate_hidden(
    stock_qty: Decimal,
    demand_by_source: Mapping[str, Decimal],
) -> tuple[dict[str, Decimal], Decimal]:
    remaining = max(ZERO, stock_qty)
    served: dict[str, Decimal] = {}
    for source in ("reserve_backlog", "site_order", "kmp4", "site_cart"):
        demand = max(ZERO, demand_by_source.get(source, ZERO))
        quantity = min(remaining, demand)
        served[source] = quantity
        remaining -= quantity
    return served, remaining


def _add_hidden_source_metrics(
    metric: Metric,
    *,
    demand_by_source: Mapping[str, Decimal],
    served_by_source: Mapping[str, Decimal],
) -> None:
    metric.hidden_kmp4_qty += demand_by_source.get("kmp4", ZERO)
    metric.hidden_site_order_qty += demand_by_source.get("site_order", ZERO)
    metric.hidden_site_cart_qty += demand_by_source.get("site_cart", ZERO)
    metric.hidden_reserve_backlog_qty += demand_by_source.get("reserve_backlog", ZERO)
    metric.served_hidden_kmp4_qty += served_by_source.get("kmp4", ZERO)
    metric.served_hidden_site_order_qty += served_by_source.get("site_order", ZERO)
    metric.served_hidden_site_cart_qty += served_by_source.get("site_cart", ZERO)
    metric.served_hidden_reserve_backlog_qty += served_by_source.get("reserve_backlog", ZERO)


def simulate_scenario(
    *,
    scenario: FrozenScenario,
    fact_rows_by_date: Mapping[date, Sequence[Mapping[str, Any]]],
    decision_rows_by_date: Mapping[date, Sequence[Mapping[str, Any]]],
    initial_pipeline_rows: Sequence[Mapping[str, Any]],
    sales_by_code: Mapping[str, Mapping[date, Decimal]],
    policy: AutoOrderPolicy,
    config: Any,
    date_from: date,
    date_to: date,
    keep_detail: bool,
    demand_sample_cache: dict[tuple[str, date, int], list[Decimal]] | None = None,
    decision_service_buffers: Mapping[tuple[date, str], Decimal] | None = None,
    hybrid_gap_arrival_quantile: str = "off",
    hybrid_gap_min_coverable_days: int = 0,
    hybrid_gap_acceleration_recent_days: int = 0,
    hybrid_gap_acceleration_baseline_days: int = 0,
    hybrid_gap_acceleration_min_recent_sales: Decimal = ZERO,
    hybrid_gap_acceleration_rate_multiplier: Decimal = ZERO,
    hybrid_gap_acceleration_require_forecast_growth: bool = False,
    acceleration_require_stock_above_min: bool = False,
    acceleration_allowed_statuses: Sequence[str] = (AssortmentStatus.SALE.value,),
    acceleration_eligible_sku_dates: set[tuple[date, str]] | None = None,
    preclassified_acceleration_rate_by_sku_date: Mapping[tuple[date, str], Decimal] | None = None,
    representation_minimums: Mapping[tuple[date, str], Decimal] | None = None,
    acceleration_lead_quantile: str = "p75",
    keep_decision_detail: bool = True,
    keep_loss_detail: bool = True,
    hybrid_gap_detail_only: bool = False,
    acceleration_detail_only: bool = False,
) -> SimulationResult:
    if (
        scenario.base_pipeline_lot_risk_boundary
        and scenario.grow_acceleration_profile != "off"
        and scenario.grow_acceleration_quantity_policy != "dynamic_minmax_shortage"
    ):
        raise ValueError("base pipeline lot risk cannot be combined with acceleration")
    normalized_acceleration_lead_quantile = _clean(acceleration_lead_quantile).lower() or "p75"
    if normalized_acceleration_lead_quantile not in {"p50", "p75"}:
        raise ValueError("acceleration lead quantile must be p50 or p75")
    normalized_hybrid_quantile = _clean(hybrid_gap_arrival_quantile).lower() or "off"
    if normalized_hybrid_quantile not in {"off", "p50", "p75"}:
        raise ValueError("hybrid gap arrival quantile must be off, p50, or p75")
    normalized_hybrid_min_days = int(hybrid_gap_min_coverable_days)
    if normalized_hybrid_min_days < 0:
        raise ValueError("hybrid gap minimum coverable days cannot be negative")
    normalized_hybrid_acceleration_recent_days = int(hybrid_gap_acceleration_recent_days)
    normalized_hybrid_acceleration_baseline_days = int(hybrid_gap_acceleration_baseline_days)
    normalized_hybrid_acceleration_min_recent_sales = max(
        ZERO, _decimal(hybrid_gap_acceleration_min_recent_sales)
    )
    normalized_hybrid_acceleration_rate_multiplier = max(
        ZERO, _decimal(hybrid_gap_acceleration_rate_multiplier)
    )
    hybrid_acceleration_filter_enabled = bool(
        normalized_hybrid_acceleration_recent_days > 0
        and normalized_hybrid_acceleration_baseline_days > 0
        and normalized_hybrid_acceleration_min_recent_sales > ZERO
        and normalized_hybrid_acceleration_rate_multiplier > ONE
    )
    normalized_acceleration_statuses = {
        _clean(value) for value in acceleration_allowed_statuses if _clean(value)
    }
    if not normalized_acceleration_statuses:
        normalized_acceleration_statuses = {AssortmentStatus.SALE.value}
    codes = sorted(
        {
            _clean(row.get("nomenclature_code"))
            for rows in fact_rows_by_date.values()
            for row in rows
            if _clean(row.get("nomenclature_code"))
        }
    )
    demand_pattern_by_code = {
        code: classify_demand_pattern(
            completed_weekly_demand(
                sales_by_code.get(code, {}),
                as_of=date_from,
            )
        ).name
        for code in codes
    }
    first_facts = {
        _clean(row.get("nomenclature_code")): row for row in fact_rows_by_date.get(date_from, ())
    }
    stock = {
        code: max(
            ZERO,
            _decimal(first_facts.get(code, {}).get("physical_stock_qty"))
            + _decimal(first_facts.get(code, {}).get("observed_sales_qty")),
        )
        for code in codes
    }
    placed = {
        code: _decimal(first_facts.get(code, {}).get("placed_incoming_qty")) for code in codes
    }
    initial = _free_initial_pipeline(
        initial_pipeline_rows,
        placed_by_code=placed,
        date_from=date_from,
    )
    early_launch_statuses = {
        AssortmentStatus.FRUIT.value,
        AssortmentStatus.NEWBORN.value,
        AssortmentStatus.NEW_ITEM.value,
        AssortmentStatus.SALES_START.value,
    }
    launch_seed_pending = {
        code
        for code in codes
        if code not in first_facts
        or (
            stock[code] <= ZERO
            and code not in initial
            and _clean(first_facts[code].get("status")) in early_launch_statuses
        )
    }
    launch_ready = {
        code
        for code in codes
        if code in first_facts
        and (
            stock[code] > ZERO
            or _clean(first_facts[code].get("status")) not in early_launch_statuses
        )
    }
    arrivals: dict[date, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    acceleration_arrivals: dict[date, dict[str, Decimal]] = defaultdict(
        lambda: defaultdict(Decimal)
    )
    hybrid_arrivals: dict[date, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    pipeline_qty: dict[str, Decimal] = defaultdict(Decimal)
    open_acceleration_protection_qty: dict[str, Decimal] = defaultdict(Decimal)
    open_hybrid_protection_qty: dict[str, Decimal] = defaultdict(Decimal)
    for code, lots in initial.items():
        for arrival, qty in lots:
            arrivals[arrival][code] += qty
            pipeline_qty[code] += qty

    actual = {code: Metric() for code in codes}
    model = {code: Metric() for code in codes}
    actual_by_stage: dict[str, Metric] = defaultdict(Metric)
    model_by_stage: dict[str, Metric] = defaultdict(Metric)
    current_cost: dict[str, Decimal] = defaultdict(Decimal)
    current_margin: dict[str, Decimal] = defaultdict(Decimal)
    demand_samples = demand_sample_cache if demand_sample_cache is not None else {}
    decision_history_by_code: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for rows in decision_rows_by_date.values():
        for row in rows:
            code = _clean(row.get("nomenclature_code"))
            if code:
                decision_history_by_code[code].append(row)
    latest_decision_rows: dict[str, Mapping[str, Any]] = {}
    last_evaluation_by_code: dict[str, dict[str, Any]] = {}
    decision_detail: list[dict[str, Any]] = []
    daily_detail: list[dict[str, Any]] = []
    loss_detail: list[dict[str, Any]] = []
    grow_target_states: dict[str, GrowProtectionState] = {}
    active_decision_service_buffer: dict[str, Decimal] = defaultdict(Decimal)
    manual_review_seen: set[str] = set()
    diagnostics = ScenarioDiagnostics()

    cursor = date_from
    while cursor <= date_to:
        released_acceleration_today: dict[str, Decimal] = defaultdict(Decimal)
        for code, qty in arrivals.get(cursor, {}).items():
            stock[code] += qty
            pipeline_qty[code] = max(ZERO, pipeline_qty[code] - qty)
            hybrid_arrived = min(
                open_hybrid_protection_qty[code],
                max(ZERO, hybrid_arrivals.get(cursor, {}).get(code, ZERO)),
            )
            if hybrid_arrived > ZERO:
                open_hybrid_protection_qty[code] = release_open_acceleration_protection(
                    open_hybrid_protection_qty[code],
                    arrived_qty=hybrid_arrived,
                )
                diagnostics.hybrid_gap_released_on_arrival_qty += hybrid_arrived
            acceleration_arrived = min(
                open_acceleration_protection_qty[code],
                max(ZERO, acceleration_arrivals.get(cursor, {}).get(code, ZERO)),
            )
            if acceleration_arrived > ZERO:
                open_acceleration_protection_qty[code] = release_open_acceleration_protection(
                    open_acceleration_protection_qty[code],
                    arrived_qty=acceleration_arrived,
                )
                released_acceleration_today[code] = acceleration_arrived
                diagnostics.acceleration_released_on_arrival_qty += acceleration_arrived
            if qty > ZERO:
                launch_ready.add(code)
        decisions_today = {
            _clean(row.get("nomenclature_code")): row
            for row in decision_rows_by_date.get(cursor, ())
        }
        latest_decision_rows.update(decisions_today)
        for code, row in decisions_today.items():
            current_cost[code] = _decimal(row.get("inventory_cost_per_unit_rub"))
            current_margin[code] = _decimal(row.get("gross_margin_per_unit_rub"))
        facts_today = {
            _clean(row.get("nomenclature_code")): row for row in fact_rows_by_date.get(cursor, ())
        }

        daily = {
            "scenario_id": scenario.scenario_id,
            "business_date": cursor.isoformat(),
            "actual_potential_demand_qty": ZERO,
            "actual_observed_demand_qty": ZERO,
            "actual_hidden_demand_qty": ZERO,
            "actual_hidden_kmp4_qty": ZERO,
            "actual_hidden_site_order_qty": ZERO,
            "actual_hidden_site_cart_qty": ZERO,
            "actual_hidden_reserve_backlog_qty": ZERO,
            "actual_served_qty": ZERO,
            "actual_served_observed_qty": ZERO,
            "actual_served_hidden_qty": ZERO,
            "model_served_qty": ZERO,
            "model_served_observed_qty": ZERO,
            "model_served_hidden_qty": ZERO,
            "model_lost_qty": ZERO,
            "model_lost_observed_qty": ZERO,
            "model_lost_hidden_qty": ZERO,
            "actual_gross_profit_rub": ZERO,
            "model_gross_profit_rub": ZERO,
            "actual_inventory_value_rub": ZERO,
            "model_ending_inventory_qty": ZERO,
            "model_inventory_value_rub": ZERO,
        }
        for fact in facts_today.values():
            code = _clean(fact.get("nomenclature_code"))
            status = _clean(fact.get("status")) or "unknown"
            observed = max(ZERO, _decimal(fact.get("observed_sales_qty")))
            hidden_by_source = {
                "kmp4": max(ZERO, _decimal(fact.get("kmp4_expired_qty"))) * scenario.kmp4_weight,
                "site_order": max(ZERO, _decimal(fact.get("site_order_hidden_qty")))
                * scenario.site_order_weight,
                "site_cart": max(ZERO, _decimal(fact.get("site_cart_hidden_qty")))
                * scenario.site_unordered_cart_weight,
                "reserve_backlog": max(ZERO, _decimal(fact.get("reserve_backlog_hidden_qty"))),
            }
            hidden = sum(hidden_by_source.values(), ZERO)
            margin = current_margin[code]
            cost = current_cost[code]

            if code in launch_seed_pending:
                launch_seed = max(
                    ZERO,
                    _decimal(fact.get("physical_stock_qty")) + observed,
                )
                if launch_seed > ZERO:
                    stock[code] += launch_seed
                    model[code].exogenous_launch_seed_qty += launch_seed
                    model_by_stage[status].exogenous_launch_seed_qty += launch_seed
                    launch_seed_pending.remove(code)
                    launch_ready.add(code)

            actual_row = actual[code]
            actual_stage = actual_by_stage[status]
            actual_stock = max(ZERO, _decimal(fact.get("physical_stock_qty")))
            actual_hidden_served_by_source, _ = _allocate_hidden(actual_stock, hidden_by_source)
            actual_hidden_served = sum(actual_hidden_served_by_source.values(), ZERO)
            actual_row.observed_demand_qty += observed
            actual_row.hidden_demand_qty += hidden
            actual_row.served_observed_qty += observed
            actual_row.served_hidden_qty += actual_hidden_served
            actual_row.lost_hidden_qty += hidden - actual_hidden_served
            actual_row.gross_profit_rub += (observed + actual_hidden_served) * margin
            actual_row.inventory_qty_days += actual_stock
            actual_row.priced_inventory_qty_days += actual_stock if cost > ZERO else ZERO
            actual_row.inventory_value_days_rub += actual_stock * cost
            actual_row.ending_inventory_qty = actual_stock
            _add_hidden_source_metrics(
                actual_row,
                demand_by_source=hidden_by_source,
                served_by_source=actual_hidden_served_by_source,
            )
            actual_stage.observed_demand_qty += observed
            actual_stage.hidden_demand_qty += hidden
            actual_stage.served_observed_qty += observed
            actual_stage.served_hidden_qty += actual_hidden_served
            actual_stage.lost_hidden_qty += hidden - actual_hidden_served
            actual_stage.gross_profit_rub += (observed + actual_hidden_served) * margin
            actual_stage.inventory_qty_days += actual_stock
            actual_stage.priced_inventory_qty_days += actual_stock if cost > ZERO else ZERO
            actual_stage.inventory_value_days_rub += actual_stock * cost
            _add_hidden_source_metrics(
                actual_stage,
                demand_by_source=hidden_by_source,
                served_by_source=actual_hidden_served_by_source,
            )

            model_row = model[code]
            model_stage = model_by_stage[status]
            model_stock_before_demand = stock[code]
            served_observed = min(stock[code], observed)
            stock[code] -= served_observed
            model_hidden_served_by_source, remaining_stock = _allocate_hidden(
                stock[code], hidden_by_source
            )
            served_hidden = sum(model_hidden_served_by_source.values(), ZERO)
            stock[code] = remaining_stock
            model_row.observed_demand_qty += observed
            model_row.hidden_demand_qty += hidden
            model_row.served_observed_qty += served_observed
            model_row.served_hidden_qty += served_hidden
            model_row.lost_observed_qty += observed - served_observed
            model_row.lost_hidden_qty += hidden - served_hidden
            model_row.gross_profit_rub += (served_observed + served_hidden) * margin
            model_row.inventory_qty_days += stock[code]
            model_row.priced_inventory_qty_days += stock[code] if cost > ZERO else ZERO
            model_row.inventory_value_days_rub += stock[code] * cost
            model_row.ending_inventory_qty = stock[code]
            lost_observed = observed - served_observed
            if keep_detail and keep_loss_detail and lost_observed > ZERO:
                prior = last_evaluation_by_code.get(code, {})
                pending_arrival_dates = [
                    arrival_date
                    for arrival_date, quantities in arrivals.items()
                    if arrival_date > cursor and quantities.get(code, ZERO) > ZERO
                ]
                next_arrival_date = min(pending_arrival_dates, default=None)
                current_input = latest_decision_rows.get(code, {})
                fresh_input = decisions_today.get(code, {})
                loss_detail.append(
                    {
                        "scenario_id": scenario.scenario_id,
                        "business_date": cursor.isoformat(),
                        "nomenclature_code": code,
                        "status": status,
                        "demand_pattern_preperiod": demand_pattern_by_code[code],
                        "observed_demand_qty": str(observed),
                        "served_observed_qty": str(served_observed),
                        "lost_observed_qty": str(lost_observed),
                        "model_stock_before_demand_qty": str(model_stock_before_demand),
                        "model_stock_after_demand_qty": str(stock[code]),
                        "actual_physical_stock_qty": str(actual_stock),
                        "raw_reserve_qty": str(_decimal(fact.get("raw_reserve_qty"))),
                        "effective_reserve_qty": str(
                            max(ZERO, _decimal(fact.get("effective_reserve_qty")))
                        ),
                        "reserve_backlog_qty": str(
                            max(ZERO, _decimal(fact.get("reserve_backlog_qty")))
                        ),
                        "model_pipeline_qty": str(pipeline_qty[code]),
                        "next_pipeline_arrival_date": (
                            next_arrival_date.isoformat() if next_arrival_date else ""
                        ),
                        "days_to_next_pipeline_arrival": (
                            (next_arrival_date - cursor).days if next_arrival_date else ""
                        ),
                        "launch_seed_pending": int(code in launch_seed_pending),
                        "launch_ready": int(code in launch_ready),
                        "inventory_cost_per_unit_rub": str(current_cost[code]),
                        "gross_margin_per_unit_rub": str(current_margin[code]),
                        "current_forecast_rate_sales": str(
                            max(ZERO, _decimal(current_input.get("forecast_rate_sales")))
                        ),
                        "current_lead_time_p50_days": int(
                            current_input.get("lead_time_p50_days") or 52
                        ),
                        "current_lead_time_p75_days": int(
                            current_input.get("lead_time_p75_days")
                            or current_input.get("lead_time_p50_days")
                            or 52
                        ),
                        "current_lead_time_confidence": _clean(
                            current_input.get("lead_time_confidence")
                        ),
                        "current_day_has_fresh_decision": int(bool(fresh_input)),
                        "current_day_scheduled_review": int(
                            _clean(fresh_input.get("scheduled_review")) == "1"
                        ),
                        "prior_evaluation_date": prior.get("evaluation_date", ""),
                        "prior_input_decision_date": prior.get("input_decision_date", ""),
                        "prior_fresh_decision": prior.get("fresh_decision", 0),
                        "prior_scheduled_review": prior.get("scheduled_review", 0),
                        "prior_forecast_rate_sales": prior.get("forecast_rate_sales", ""),
                        "prior_selected_lead_time_days": prior.get("selected_lead_time_days", ""),
                        "prior_simulated_arrival_lead_time_days": prior.get(
                            "simulated_arrival_lead_time_days", ""
                        ),
                        "prior_lead_time_p75_days": prior.get("lead_time_p75_days", ""),
                        "prior_lead_time_confidence": prior.get("lead_time_confidence", ""),
                        "prior_min_stock_qty": prior.get("min_stock_qty", ""),
                        "prior_max_stock_qty": prior.get("max_stock_qty", ""),
                        "prior_safety_stock_qty": prior.get("safety_stock_qty", ""),
                        "prior_target_stock_qty": prior.get("target_stock_qty", ""),
                        "prior_model_stock_qty": prior.get("model_stock_qty", ""),
                        "prior_reserve_qty": prior.get("reserve_qty", ""),
                        "prior_model_pipeline_qty": prior.get("model_pipeline_qty", ""),
                        "prior_effective_model_pipeline_qty": prior.get(
                            "effective_model_pipeline_qty", ""
                        ),
                        "prior_base_pipeline_profile": prior.get("base_pipeline_profile", ""),
                        "prior_base_pipeline_fraction": prior.get("base_pipeline_fraction", ""),
                        "prior_base_pipeline_margin_to_cost_ratio": prior.get(
                            "base_pipeline_margin_to_cost_ratio", ""
                        ),
                        "prior_base_pipeline_lot_risk_boundary": prior.get(
                            "base_pipeline_lot_risk_boundary", ""
                        ),
                        "prior_base_pipeline_lot_risk_boundary_days": prior.get(
                            "base_pipeline_lot_risk_boundary_days", ""
                        ),
                        "prior_base_pipeline_lot_risky_qty": prior.get(
                            "base_pipeline_lot_risky_qty", ""
                        ),
                        "prior_inventory_position_qty": prior.get("inventory_position_qty", ""),
                        "prior_triggered": prior.get("triggered", 0),
                        "prior_recommended_order_qty_raw": prior.get(
                            "recommended_order_qty_raw", ""
                        ),
                        "prior_recommended_order_qty": prior.get("recommended_order_qty", ""),
                        "prior_expected_arrival_date": prior.get("expected_arrival_date", ""),
                    }
                )
            _add_hidden_source_metrics(
                model_row,
                demand_by_source=hidden_by_source,
                served_by_source=model_hidden_served_by_source,
            )
            model_stage.observed_demand_qty += observed
            model_stage.hidden_demand_qty += hidden
            model_stage.served_observed_qty += served_observed
            model_stage.served_hidden_qty += served_hidden
            model_stage.lost_observed_qty += observed - served_observed
            model_stage.lost_hidden_qty += hidden - served_hidden
            model_stage.gross_profit_rub += (served_observed + served_hidden) * margin
            model_stage.inventory_qty_days += stock[code]
            model_stage.priced_inventory_qty_days += stock[code] if cost > ZERO else ZERO
            model_stage.inventory_value_days_rub += stock[code] * cost
            _add_hidden_source_metrics(
                model_stage,
                demand_by_source=hidden_by_source,
                served_by_source=model_hidden_served_by_source,
            )

            daily["actual_potential_demand_qty"] += observed + hidden
            daily["actual_observed_demand_qty"] += observed
            daily["actual_hidden_demand_qty"] += hidden
            daily["actual_hidden_kmp4_qty"] += hidden_by_source["kmp4"]
            daily["actual_hidden_site_order_qty"] += hidden_by_source["site_order"]
            daily["actual_hidden_site_cart_qty"] += hidden_by_source["site_cart"]
            daily["actual_hidden_reserve_backlog_qty"] += hidden_by_source["reserve_backlog"]
            daily["actual_served_qty"] += observed + actual_hidden_served
            daily["actual_served_observed_qty"] += observed
            daily["actual_served_hidden_qty"] += actual_hidden_served
            daily["model_served_qty"] += served_observed + served_hidden
            daily["model_served_observed_qty"] += served_observed
            daily["model_served_hidden_qty"] += served_hidden
            daily["model_lost_qty"] += observed - served_observed + hidden - served_hidden
            daily["model_lost_observed_qty"] += observed - served_observed
            daily["model_lost_hidden_qty"] += hidden - served_hidden
            daily["model_ending_inventory_qty"] += stock[code]
            daily["model_inventory_value_rub"] += stock[code] * cost
            daily["actual_inventory_value_rub"] += actual_stock * cost
            daily["actual_gross_profit_rub"] += (observed + actual_hidden_served) * margin
            daily["model_gross_profit_rub"] += (served_observed + served_hidden) * margin

        decision_candidates = decisions_today if scenario.legacy else latest_decision_rows
        service_floor_allocations: dict[str, Decimal] = {}
        if (
            scenario.grow_service_floor_percentile > ZERO
            and scenario.grow_service_floor_stage_budget_rub > ZERO
        ):
            budget_candidates: list[ServiceFloorCandidate] = []
            for candidate_code, candidate_row in sorted(decision_candidates.items()):
                candidate_fact = facts_today.get(candidate_code, {})
                candidate_status = _clean(candidate_fact.get("status")) or _clean(
                    candidate_row.get("status")
                )
                if candidate_status != AssortmentStatus.SALE.value:
                    continue
                candidate_lead_days = int(candidate_row.get("lead_time_p50_days") or 52)
                candidate_cache_key = (
                    candidate_code,
                    cursor,
                    candidate_lead_days + policy.order_cadence_days,
                )
                candidate_samples = demand_samples.get(candidate_cache_key)
                if candidate_samples is None:
                    candidate_samples = historical_forecast_error_samples(
                        decision_history_by_code.get(candidate_code, ()),
                        sales_by_code.get(candidate_code, {}),
                        as_of=cursor,
                        order_cadence_days=policy.order_cadence_days,
                        lookback_days=config.safety_lookback_days,
                    )
                    demand_samples[candidate_cache_key] = candidate_samples
                requested_units = min(
                    Decimal(config.safety_max_units),
                    empirical_underforecast_percentile(
                        candidate_samples,
                        percentile=scenario.grow_service_floor_percentile,
                        min_samples=config.safety_min_samples,
                    ),
                )
                capped_units = apply_service_floor_sku_cap(
                    requested_units,
                    unit_cost_rub=current_cost[candidate_code],
                    per_sku_cap_rub=scenario.grow_service_floor_sku_cap_rub,
                )
                if capped_units <= ZERO:
                    service_floor_allocations[candidate_code] = ZERO
                    continue
                budget_candidates.append(
                    ServiceFloorCandidate(
                        code=candidate_code,
                        requested_units=capped_units,
                        unit_cost_rub=current_cost[candidate_code],
                        gross_margin_per_unit_rub=current_margin[candidate_code],
                        error_samples=tuple(candidate_samples),
                    )
                )
            service_floor_allocations.update(
                allocate_service_floor_budget(
                    budget_candidates,
                    stage_budget_rub=scenario.grow_service_floor_stage_budget_rub,
                )
            )
            for candidate in budget_candidates:
                allocated_qty = service_floor_allocations.get(candidate.code, ZERO)
                diagnostics.service_floor_positive_recalculations += 1
                diagnostics.service_floor_requested_qty += candidate.requested_units
                diagnostics.service_floor_allocated_qty += allocated_qty
                diagnostics.service_floor_budget_limited_recalculations += int(
                    allocated_qty < candidate.requested_units
                )
        acceleration_context: dict[str, dict[str, Any]] = {}
        acceleration_allocations: dict[str, Decimal] = {}
        if (
            scenario.grow_acceleration_recent_days > 0
            and scenario.grow_acceleration_stage_budget_rub > ZERO
        ):
            acceleration_candidates: list[ServiceFloorCandidate] = []
            for candidate_code, candidate_row in sorted(decision_candidates.items()):
                candidate_fact = facts_today.get(candidate_code, {})
                candidate_status = _clean(candidate_fact.get("status")) or _clean(
                    candidate_row.get("status")
                )
                if candidate_status not in normalized_acceleration_statuses:
                    continue
                if (
                    acceleration_eligible_sku_dates is not None
                    and (cursor, candidate_code) not in acceleration_eligible_sku_dates
                ):
                    continue
                signal = calculate_demand_acceleration(
                    sales_by_code.get(candidate_code, {}),
                    as_of=cursor,
                    recent_days=scenario.grow_acceleration_recent_days,
                    baseline_days=scenario.grow_acceleration_baseline_days,
                    min_recent_sales=scenario.grow_acceleration_min_recent_sales,
                    rate_multiplier=scenario.grow_acceleration_rate_multiplier,
                )
                preclassified_rate = max(
                    ZERO,
                    _decimal(
                        (preclassified_acceleration_rate_by_sku_date or {}).get(
                            (cursor, candidate_code)
                        )
                    ),
                )
                if preclassified_rate > ZERO:
                    signal = DemandAccelerationSignal(
                        recent_qty=preclassified_rate
                        * Decimal(max(1, scenario.grow_acceleration_recent_days)),
                        baseline_qty=ZERO,
                        recent_rate=preclassified_rate,
                        baseline_rate=ZERO,
                        rate_ratio=Decimal("999"),
                        triggered=True,
                    )
                context: dict[str, Any] = {
                    "signal": signal,
                    "uncapped_requested_qty": ZERO,
                    "requested_qty": ZERO,
                    "sku_capped_qty": ZERO,
                }
                acceleration_context[candidate_code] = context
                candidate_rate = max(
                    ZERO,
                    _decimal(candidate_row.get("forecast_rate_sales")),
                )
                candidate_lead_days = int(
                    candidate_row.get(f"lead_time_{normalized_acceleration_lead_quantile}_days")
                    or candidate_row.get("lead_time_p50_days")
                    or 52
                )
                candidate_reserve = max(
                    ZERO,
                    _decimal(
                        candidate_fact.get(
                            "effective_reserve_qty",
                            candidate_row.get(
                                "effective_reserve_qty",
                                candidate_row.get("reserve_qty"),
                            ),
                        )
                    ),
                )
                candidate_pipeline_fraction = acceleration_pipeline_fraction(
                    _clean(candidate_row.get("lead_time_confidence")),
                    medium_fraction=scenario.grow_acceleration_medium_pipeline_fraction,
                    low_fraction=scenario.grow_acceleration_low_pipeline_fraction,
                )
                candidate_base_pipeline_fraction = base_pipeline_fraction(
                    _clean(candidate_row.get("lead_time_confidence")),
                    high_fraction=scenario.base_pipeline_high_fraction,
                    medium_fraction=scenario.base_pipeline_medium_fraction,
                    low_fraction=scenario.base_pipeline_low_fraction,
                    min_margin_to_cost_ratio=(scenario.base_pipeline_min_margin_to_cost_ratio),
                    gross_margin_per_unit_rub=current_margin[candidate_code],
                    inventory_cost_per_unit_rub=current_cost[candidate_code],
                )
                candidate_open_acceleration = (
                    open_acceleration_protection_qty[candidate_code]
                    if scenario.grow_acceleration_single_open_lot
                    else ZERO
                )
                candidate_ordinary_pipeline = max(
                    ZERO,
                    pipeline_qty[candidate_code] - candidate_open_acceleration,
                )
                acceleration_guard_days = candidate_lead_days
                if scenario.grow_acceleration_quantity_policy == "dynamic_minmax_shortage":
                    acceleration_guard_days += policy.order_cadence_days
                guard = evaluate_acceleration_shortage_guard(
                    signal=signal,
                    forecast_rate=candidate_rate,
                    lead_time_days=acceleration_guard_days,
                    model_stock_qty=stock[candidate_code],
                    effective_reserve_qty=candidate_reserve,
                    effective_pipeline_qty=(
                        candidate_ordinary_pipeline
                        * min(candidate_pipeline_fraction, candidate_base_pipeline_fraction)
                        if scenario.grow_acceleration_single_open_lot
                        else pipeline_qty[candidate_code]
                        * min(candidate_pipeline_fraction, candidate_base_pipeline_fraction)
                    ),
                    open_acceleration_protection_qty=candidate_open_acceleration,
                    require_forecast_growth=(scenario.grow_acceleration_require_forecast_growth),
                    min_shortage_qty=scenario.grow_acceleration_min_shortage_qty,
                )
                candidate_static_min_qty = _ceil(candidate_rate * Decimal(candidate_lead_days))
                candidate_free_stock_qty = max(
                    ZERO,
                    stock[candidate_code] - candidate_reserve,
                )
                stock_above_min_passed = bool(candidate_free_stock_qty > candidate_static_min_qty)
                context["guard"] = guard
                context["static_min_qty"] = candidate_static_min_qty
                context["free_stock_qty"] = candidate_free_stock_qty
                context["stock_above_min_passed"] = stock_above_min_passed
                segment_gate = evaluate_acceleration_segment_gate(
                    demand_pattern=demand_pattern_by_code[candidate_code],
                    unit_cost_rub=current_cost[candidate_code],
                    lead_time_confidence=_clean(candidate_row.get("lead_time_confidence")),
                    lead_time_p75_days=candidate_lead_days,
                    allowed_demand_patterns=(scenario.grow_acceleration_allowed_demand_patterns),
                    max_unit_cost_rub=(scenario.grow_acceleration_max_unit_cost_rub),
                    allowed_lead_confidences=(scenario.grow_acceleration_allowed_lead_confidences),
                    max_p75_days=scenario.grow_acceleration_max_p75_days,
                )
                context["segment_gate"] = segment_gate
                if (
                    not guard.eligible
                    or not segment_gate.eligible
                    or (acceleration_require_stock_above_min and not stock_above_min_passed)
                ):
                    continue
                candidate_cache_key = (
                    candidate_code,
                    cursor,
                    candidate_lead_days + policy.order_cadence_days,
                )
                candidate_samples = demand_samples.get(candidate_cache_key)
                if candidate_samples is None:
                    candidate_samples = historical_forecast_error_samples(
                        decision_history_by_code.get(candidate_code, ()),
                        sales_by_code.get(candidate_code, {}),
                        as_of=cursor,
                        order_cadence_days=policy.order_cadence_days,
                        lookback_days=config.safety_lookback_days,
                    )
                    demand_samples[candidate_cache_key] = candidate_samples
                candidate_economic_cap = calculate_economic_safety_stock(
                    base_max_qty=ZERO,
                    demand_samples=candidate_samples,
                    gross_margin_per_unit_rub=current_margin[candidate_code],
                    inventory_cost_per_unit_rub=current_cost[candidate_code],
                    holding_days=candidate_lead_days + policy.order_cadence_days,
                    cost_scenario=scenario.cost,
                    max_units=config.safety_max_units,
                    min_samples=config.safety_min_samples,
                ).units
                candidate_percentile_target = empirical_underforecast_percentile(
                    candidate_samples,
                    percentile=scenario.forecast_error_percentile,
                    min_samples=config.safety_min_samples,
                )
                candidate_ordinary_safety = min(
                    candidate_economic_cap,
                    candidate_percentile_target,
                )
                if scenario.grow_acceleration_quantity_policy == "dynamic_minmax_shortage":
                    uncapped_requested_units = guard.projected_shortage_qty
                else:
                    uncapped_requested_units = acceleration_incremental_units(
                        signal=signal,
                        forecast_rate=candidate_rate,
                        coverage_days=candidate_lead_days + policy.order_cadence_days,
                        percentile_safety_units=candidate_percentile_target,
                        ordinary_safety_units=candidate_ordinary_safety,
                        max_units=config.safety_max_units,
                    )
                requested_units = cap_acceleration_to_projected_shortage(
                    uncapped_requested_units,
                    projected_shortage_qty=guard.projected_shortage_qty,
                    enabled=scenario.grow_acceleration_cap_to_projected_shortage,
                )
                capped_units = apply_service_floor_sku_cap(
                    requested_units,
                    unit_cost_rub=current_cost[candidate_code],
                    per_sku_cap_rub=scenario.grow_acceleration_sku_cap_rub,
                )
                context["uncapped_requested_qty"] = uncapped_requested_units
                context["requested_qty"] = requested_units
                context["sku_capped_qty"] = capped_units
                if capped_units <= ZERO:
                    acceleration_allocations[candidate_code] = ZERO
                    continue
                acceleration_candidates.append(
                    ServiceFloorCandidate(
                        code=candidate_code,
                        requested_units=capped_units,
                        unit_cost_rub=current_cost[candidate_code],
                        gross_margin_per_unit_rub=current_margin[candidate_code],
                        error_samples=tuple(candidate_samples),
                    )
                )
            acceleration_allocations.update(
                allocate_service_floor_budget(
                    acceleration_candidates,
                    stage_budget_rub=scenario.grow_acceleration_stage_budget_rub,
                )
            )
            for candidate_code, context in acceleration_context.items():
                signal = context["signal"]
                guard = context["guard"]
                segment_gate = context["segment_gate"]
                uncapped_requested_qty = context["uncapped_requested_qty"]
                requested_qty = context["requested_qty"]
                sku_capped_qty = context["sku_capped_qty"]
                allocated_qty = acceleration_allocations.get(candidate_code, ZERO)
                diagnostics.acceleration_triggered_recalculations += int(signal.triggered)
                diagnostics.acceleration_forecast_growth_passed_recalculations += int(
                    signal.triggered and guard.forecast_growth_passed
                )
                diagnostics.acceleration_shortage_passed_recalculations += int(
                    signal.triggered and guard.shortage_passed
                )
                diagnostics.acceleration_guard_eligible_recalculations += int(guard.eligible)
                if guard.eligible:
                    diagnostics.acceleration_segment_evaluated_recalculations += 1
                    diagnostics.acceleration_segment_passed_recalculations += int(
                        segment_gate.eligible
                    )
                    diagnostics.acceleration_segment_blocked_recalculations += int(
                        not segment_gate.eligible
                    )
                    diagnostics.acceleration_segment_blocked_pattern_recalculations += int(
                        not segment_gate.pattern_passed
                    )
                    diagnostics.acceleration_segment_blocked_cost_recalculations += int(
                        not segment_gate.cost_passed
                    )
                    diagnostics.acceleration_segment_blocked_confidence_recalculations += int(
                        not segment_gate.confidence_passed
                    )
                    diagnostics.acceleration_segment_blocked_p75_recalculations += int(
                        not segment_gate.p75_passed
                    )
                shortage_threshold = max(
                    ZERO,
                    scenario.grow_acceleration_min_shortage_qty,
                )
                gross_shortage_passed = bool(
                    shortage_threshold <= ZERO
                    or guard.gross_projected_shortage_qty >= shortage_threshold
                )
                blocked_by_open_lot = bool(
                    scenario.grow_acceleration_single_open_lot
                    and signal.triggered
                    and guard.forecast_growth_passed
                    and gross_shortage_passed
                    and not guard.shortage_passed
                )
                diagnostics.acceleration_single_open_blocked_recalculations += int(
                    blocked_by_open_lot
                )
                if blocked_by_open_lot:
                    diagnostics.acceleration_single_open_blocked_qty += max(
                        ZERO,
                        guard.gross_projected_shortage_qty - guard.projected_shortage_qty,
                    )
                diagnostics.acceleration_uncapped_requested_qty += uncapped_requested_qty
                diagnostics.acceleration_shortage_cap_reduction_qty += max(
                    ZERO,
                    uncapped_requested_qty - requested_qty,
                )
                diagnostics.acceleration_shortage_capped_recalculations += int(
                    requested_qty < uncapped_requested_qty
                )
                if requested_qty <= ZERO:
                    continue
                diagnostics.acceleration_positive_recalculations += 1
                diagnostics.acceleration_requested_qty += requested_qty
                diagnostics.acceleration_allocated_qty += allocated_qty
                diagnostics.acceleration_budget_limited_recalculations += int(
                    allocated_qty < sku_capped_qty
                )
        for code, row in decision_candidates.items():
            fact = facts_today.get(code, {})
            if not fact:
                continue
            status = _clean(fact.get("status")) or _clean(row.get("status"))
            fresh_decision = code in decisions_today
            scheduled_review = fresh_decision and _clean(row.get("scheduled_review")) == "1"
            if scenario.legacy and not scheduled_review:
                continue
            rate = max(ZERO, _decimal(row.get("forecast_rate_sales")))
            weighted_kmp = (
                max(
                    ZERO,
                    _decimal(fact.get("kmp4_open_qty", row.get("kmp4_open_qty"))),
                )
                * scenario.kmp4_weight
            )
            weighted_site_orders = (
                max(
                    ZERO,
                    _decimal(fact.get("site_order_open_qty", row.get("site_order_open_qty"))),
                )
                * scenario.site_order_weight
            )
            weighted_site_carts = (
                max(
                    ZERO,
                    _decimal(fact.get("site_cart_open_qty", row.get("site_cart_open_qty"))),
                )
                * scenario.site_unordered_cart_weight
            )
            weighted_signals = weighted_kmp + weighted_site_orders + weighted_site_carts
            scenario_rate = rate
            arrival_lead_days = 52 if scenario.legacy else int(row.get("lead_time_p50_days") or 52)
            lead_days = arrival_lead_days
            base_pipeline_share = base_pipeline_fraction(
                _clean(row.get("lead_time_confidence")),
                high_fraction=scenario.base_pipeline_high_fraction,
                medium_fraction=scenario.base_pipeline_medium_fraction,
                low_fraction=scenario.base_pipeline_low_fraction,
                min_margin_to_cost_ratio=(scenario.base_pipeline_min_margin_to_cost_ratio),
                gross_margin_per_unit_rub=current_margin[code],
                inventory_cost_per_unit_rub=current_cost[code],
            )
            base_pipeline_lot_risk_boundary_days = 0
            if base_pipeline_share < ONE:
                if scenario.base_pipeline_lot_risk_boundary == "p50":
                    base_pipeline_lot_risk_boundary_days = int(row.get("lead_time_p50_days") or 52)
                elif scenario.base_pipeline_lot_risk_boundary == "p75":
                    base_pipeline_lot_risk_boundary_days = int(
                        row.get("lead_time_p75_days") or row.get("lead_time_p50_days") or 52
                    )
            risk_adjusted_base_pipeline_qty, base_pipeline_lot_risky_qty = (
                risk_adjusted_base_pipeline_quantity(
                    total_pipeline_qty=pipeline_qty[code],
                    base_fraction=base_pipeline_share,
                    arrivals=arrivals,
                    code=code,
                    as_of=cursor,
                    boundary_days=base_pipeline_lot_risk_boundary_days,
                    risk_fraction=scenario.base_pipeline_lot_risk_fraction,
                )
            )
            if scenario.base_pipeline_lot_risk_boundary and base_pipeline_share < ONE:
                diagnostics.base_pipeline_lot_risk_evaluations += 1
                diagnostics.base_pipeline_lot_risk_positive_evaluations += int(
                    base_pipeline_lot_risky_qty > ZERO
                )
                diagnostics.base_pipeline_lot_risk_qty_evaluated += base_pipeline_lot_risky_qty
                diagnostics.base_pipeline_lot_risk_effective_reduction_qty += max(
                    ZERO,
                    pipeline_qty[code] * base_pipeline_share - risk_adjusted_base_pipeline_qty,
                )
            safety_units = ZERO
            economic_safety_cap = ZERO
            percentile_safety_target = ZERO
            service_floor_requested = ZERO
            service_floor_sku_capped = ZERO
            service_floor_allocated = ZERO
            acceleration_row = acceleration_context.get(code, {})
            acceleration_signal = acceleration_row.get(
                "signal",
                DemandAccelerationSignal(ZERO, ZERO, ZERO, ZERO, ZERO, False),
            )
            acceleration_guard = acceleration_row.get(
                "guard",
                AccelerationShortageGuard(False, ZERO, ZERO, ZERO, False, False),
            )
            acceleration_segment_gate = acceleration_row.get(
                "segment_gate",
                AccelerationSegmentGate(
                    demand_pattern_by_code[code],
                    True,
                    True,
                    True,
                    True,
                    True,
                ),
            )
            acceleration_uncapped_requested = _decimal(
                acceleration_row.get("uncapped_requested_qty")
            )
            acceleration_requested = _decimal(acceleration_row.get("requested_qty"))
            acceleration_sku_capped = _decimal(acceleration_row.get("sku_capped_qty"))
            acceleration_allocated = _decimal(acceleration_allocations.get(code))
            acceleration_static_min_qty = _decimal(acceleration_row.get("static_min_qty"))
            acceleration_free_stock_qty = _decimal(acceleration_row.get("free_stock_qty"))
            acceleration_stock_above_min_passed = bool(
                acceleration_row.get("stock_above_min_passed")
            )
            acceleration_pipeline_share = ONE
            acceleration_base_min_qty: Decimal | None = None
            acceleration_base_max_qty: Decimal | None = None
            acceleration_order_component = ZERO
            acceleration_open_before = open_acceleration_protection_qty[code]
            acceleration_open_after = acceleration_open_before
            ordinary_recommended = ZERO
            hybrid_evaluation = HybridGapEvaluation(
                ZERO,
                None,
                None,
                0,
                ZERO,
                ZERO,
                ZERO,
                open_hybrid_protection_qty[code],
                False,
            )
            hybrid_acceleration_signal = DemandAccelerationSignal(
                ZERO, ZERO, ZERO, ZERO, ZERO, False
            )
            hybrid_acceleration_passed = not hybrid_acceleration_filter_enabled
            hybrid_requested = ZERO
            hybrid_order_component = ZERO
            grow_protection_reason = "none"
            manual = not scheduled_review

            if scheduled_review:
                active_decision_service_buffer[code] = max(
                    ZERO,
                    _decimal((decision_service_buffers or {}).get((cursor, code))),
                )
                if active_decision_service_buffer[code] > ZERO:
                    diagnostics.decision_service_buffer_positive_decisions += 1
                    diagnostics.decision_service_buffer_requested_qty += (
                        active_decision_service_buffer[code]
                    )
            decision_service_buffer = (
                active_decision_service_buffer[code]
                if status == AssortmentStatus.SALE.value
                else ZERO
            )
            if status != AssortmentStatus.SALE.value:
                active_decision_service_buffer[code] = ZERO

            if scenario.legacy:
                if status in {
                    AssortmentStatus.FRUIT.value,
                    AssortmentStatus.NEWBORN.value,
                    AssortmentStatus.NEW_ITEM.value,
                }:
                    min_qty = ZERO
                    max_qty = ZERO
                else:
                    min_qty = _ceil(rate * Decimal(lead_days))
                    safety_days = 10 if status == AssortmentStatus.SALE.value else 0
                    max_qty = _ceil(
                        rate * Decimal(lead_days + policy.order_cadence_days + safety_days)
                    )
                    if status == AssortmentStatus.SALES_START.value:
                        remaining = max(ZERO, Decimal("12") - _decimal(row.get("sales_180")))
                        min_qty = min(min_qty, remaining)
                        max_qty = min(max_qty, remaining)
            else:
                if status == AssortmentStatus.NEW_ITEM.value:
                    scenario_rate = max(
                        scenario_rate,
                        _profile_value(row, scenario.stage_profile, "demand_qty_30d")
                        / Decimal("30"),
                    )
                min_qty = _ceil(scenario_rate * Decimal(lead_days) + weighted_signals)
                max_qty = _ceil(
                    scenario_rate * Decimal(lead_days + policy.order_cadence_days)
                    + weighted_signals
                )
                if status == AssortmentStatus.NEW_ITEM.value:
                    min_qty = max(
                        min_qty,
                        _profile_value(row, scenario.stage_profile, "min_qty"),
                    )
                    max_qty = max(
                        max_qty,
                        _profile_value(row, scenario.stage_profile, "max_qty"),
                    )
                if status in {
                    AssortmentStatus.SALE.value,
                    AssortmentStatus.WORKING.value,
                }:
                    cache_key = (code, cursor, lead_days + policy.order_cadence_days)
                    samples = demand_samples.get(cache_key)
                    if samples is None:
                        samples = historical_forecast_error_samples(
                            decision_history_by_code.get(code, ()),
                            sales_by_code.get(code, {}),
                            as_of=cursor,
                            order_cadence_days=policy.order_cadence_days,
                            lookback_days=config.safety_lookback_days,
                        )
                        demand_samples[cache_key] = samples
                    safety = calculate_economic_safety_stock(
                        base_max_qty=ZERO,
                        demand_samples=samples,
                        gross_margin_per_unit_rub=current_margin[code],
                        inventory_cost_per_unit_rub=current_cost[code],
                        holding_days=lead_days + policy.order_cadence_days,
                        cost_scenario=scenario.cost,
                        max_units=config.safety_max_units,
                        min_samples=config.safety_min_samples,
                    )
                    economic_safety_cap = safety.units
                    percentile_safety_target = empirical_underforecast_percentile(
                        samples,
                        percentile=scenario.forecast_error_percentile,
                        min_samples=config.safety_min_samples,
                    )
                    if (
                        status == AssortmentStatus.SALE.value
                        and scenario.grow_service_floor_percentile > ZERO
                    ):
                        service_floor_requested = min(
                            Decimal(config.safety_max_units),
                            empirical_underforecast_percentile(
                                samples,
                                percentile=scenario.grow_service_floor_percentile,
                                min_samples=config.safety_min_samples,
                            ),
                        )
                        service_floor_sku_capped = apply_service_floor_sku_cap(
                            service_floor_requested,
                            unit_cost_rub=current_cost[code],
                            per_sku_cap_rub=scenario.grow_service_floor_sku_cap_rub,
                        )
                        service_floor_allocated = (
                            service_floor_allocations.get(code, ZERO)
                            if scenario.grow_service_floor_stage_budget_rub > ZERO
                            else service_floor_sku_capped
                        )
                        safety_units = combine_service_floor_with_economic_stock(
                            service_floor_units=service_floor_allocated,
                            economic_cap_units=economic_safety_cap,
                            economic_percentile_target_units=percentile_safety_target,
                        )
                    elif (
                        status == AssortmentStatus.SALE.value
                        and scenario.forecast_error_percentile > ZERO
                    ):
                        safety_units = min(economic_safety_cap, percentile_safety_target)
                    else:
                        safety_units = economic_safety_cap
                    p75_days = int(row.get("lead_time_p75_days") or lead_days)
                    if safety_units > ZERO and p75_days > lead_days:
                        lead_days = p75_days
                        min_qty = _ceil(scenario_rate * Decimal(lead_days) + weighted_signals)
                        max_qty = _ceil(
                            scenario_rate * Decimal(lead_days + policy.order_cadence_days)
                            + weighted_signals
                        )
                        p75_cache_key = (
                            code,
                            cursor,
                            lead_days + policy.order_cadence_days,
                        )
                        p75_samples = demand_samples.get(p75_cache_key)
                        if p75_samples is None:
                            p75_samples = historical_forecast_error_samples(
                                decision_history_by_code.get(code, ()),
                                sales_by_code.get(code, {}),
                                as_of=cursor,
                                order_cadence_days=policy.order_cadence_days,
                                lookback_days=config.safety_lookback_days,
                            )
                            demand_samples[p75_cache_key] = p75_samples
                        economic_safety_cap = calculate_economic_safety_stock(
                            base_max_qty=ZERO,
                            demand_samples=p75_samples,
                            gross_margin_per_unit_rub=current_margin[code],
                            inventory_cost_per_unit_rub=current_cost[code],
                            holding_days=lead_days + policy.order_cadence_days,
                            cost_scenario=scenario.cost,
                            max_units=config.safety_max_units,
                            min_samples=config.safety_min_samples,
                        ).units
                        percentile_safety_target = empirical_underforecast_percentile(
                            p75_samples,
                            percentile=scenario.forecast_error_percentile,
                            min_samples=config.safety_min_samples,
                        )
                        if (
                            status == AssortmentStatus.SALE.value
                            and scenario.grow_service_floor_percentile > ZERO
                        ):
                            service_floor_requested = min(
                                Decimal(config.safety_max_units),
                                empirical_underforecast_percentile(
                                    p75_samples,
                                    percentile=scenario.grow_service_floor_percentile,
                                    min_samples=config.safety_min_samples,
                                ),
                            )
                            service_floor_sku_capped = apply_service_floor_sku_cap(
                                service_floor_requested,
                                unit_cost_rub=current_cost[code],
                                per_sku_cap_rub=scenario.grow_service_floor_sku_cap_rub,
                            )
                            service_floor_allocated = (
                                service_floor_allocations.get(code, ZERO)
                                if scenario.grow_service_floor_stage_budget_rub > ZERO
                                else service_floor_sku_capped
                            )
                            safety_units = combine_service_floor_with_economic_stock(
                                service_floor_units=service_floor_allocated,
                                economic_cap_units=economic_safety_cap,
                                economic_percentile_target_units=percentile_safety_target,
                            )
                        elif (
                            status == AssortmentStatus.SALE.value
                            and scenario.forecast_error_percentile > ZERO
                        ):
                            safety_units = min(economic_safety_cap, percentile_safety_target)
                        else:
                            safety_units = economic_safety_cap
                if (
                    status in normalized_acceleration_statuses
                    and scenario.grow_acceleration_recent_days > 0
                    and acceleration_guard.eligible
                    and acceleration_allocated > ZERO
                ):
                    lead_days = max(
                        lead_days,
                        int(
                            row.get(f"lead_time_{normalized_acceleration_lead_quantile}_days")
                            or lead_days
                        ),
                    )
                    acceleration_base_min_qty = _ceil(
                        scenario_rate * Decimal(lead_days) + weighted_signals
                    )
                    acceleration_base_max_qty = _ceil(
                        scenario_rate * Decimal(lead_days + policy.order_cadence_days)
                        + weighted_signals
                    )
                    if not scenario.grow_acceleration_single_open_lot:
                        min_qty = acceleration_base_min_qty + acceleration_allocated
                        max_qty = acceleration_base_max_qty + acceleration_allocated
                    acceleration_pipeline_share = acceleration_pipeline_fraction(
                        _clean(row.get("lead_time_confidence")),
                        medium_fraction=(scenario.grow_acceleration_medium_pipeline_fraction),
                        low_fraction=scenario.grow_acceleration_low_pipeline_fraction,
                    )
                    manual = True
                if status == AssortmentStatus.FRUIT.value:
                    min_qty = ZERO
                    max_qty = ZERO
                    safety_units = ZERO
                elif status == AssortmentStatus.NEWBORN.value:
                    min_qty = weighted_signals
                    max_qty = weighted_signals
                    safety_units = ZERO
                    manual = weighted_signals > ZERO
                elif status == AssortmentStatus.NEW_ITEM.value and code not in launch_ready:
                    min_qty = weighted_signals
                    max_qty = weighted_signals
                    safety_units = ZERO
                    manual = weighted_signals > ZERO
                elif status in {
                    AssortmentStatus.NEW_ITEM.value,
                    AssortmentStatus.SALES_START.value,
                }:
                    manual = True
                if _clean(row.get("lead_time_confidence")) == "low":
                    manual = True

            if status not in {
                AssortmentStatus.NEWBORN.value,
                AssortmentStatus.NEW_ITEM.value,
                AssortmentStatus.SALES_START.value,
                AssortmentStatus.SALE.value,
                AssortmentStatus.WORKING.value,
            }:
                min_qty = ZERO
                max_qty = ZERO
                safety_units = ZERO
                manual = False

            unprotected_min_qty = min_qty
            unprotected_max_qty = max_qty
            if status == AssortmentStatus.SALE.value and (
                scenario.grow_weekly_reduction_cap > ZERO
                or scenario.grow_entry_protection_weeks > 0
            ):
                previous_status = _clean(fact.get("previous_status"))
                entered_today = bool(
                    previous_status and previous_status != AssortmentStatus.SALE.value
                )
                min_qty, max_qty, state, grow_protection_reason = apply_grow_target_protection(
                    raw_min_qty=min_qty,
                    raw_max_qty=max_qty,
                    as_of=cursor,
                    scheduled_review=scheduled_review,
                    entered_today=entered_today,
                    weekly_reduction_cap=scenario.grow_weekly_reduction_cap,
                    entry_protection_weeks=scenario.grow_entry_protection_weeks,
                    state=grow_target_states.get(code),
                )
                grow_target_states[code] = state
            elif status != AssortmentStatus.SALE.value:
                grow_target_states.pop(code, None)

            representation_minimum_qty = (
                max(
                    ZERO,
                    _decimal((representation_minimums or {}).get((cursor, code))),
                )
                if status == AssortmentStatus.SALE.value
                else ZERO
            )
            if representation_minimum_qty > ZERO:
                min_qty = max(min_qty, representation_minimum_qty)
                max_qty = max(max_qty, representation_minimum_qty)

            min_qty += decision_service_buffer
            max_qty += decision_service_buffer

            ordinary_min_qty = min_qty
            ordinary_max_qty = max_qty
            target_qty = max_qty + safety_units
            reserve = max(
                ZERO,
                _decimal(
                    fact.get(
                        "effective_reserve_qty",
                        row.get("effective_reserve_qty", row.get("reserve_qty")),
                    )
                ),
            )
            if (
                scenario.grow_acceleration_single_open_lot
                and acceleration_base_min_qty is not None
                and acceleration_base_max_qty is not None
            ):
                ordinary_position = stock[code] - reserve + pipeline_qty[code] * base_pipeline_share
                ordinary_triggered = ordinary_position <= ordinary_min_qty
                ordinary_raw = (
                    _ceil(
                        max(
                            ZERO,
                            ordinary_max_qty + safety_units - ordinary_position,
                        )
                    )
                    if ordinary_triggered
                    else ZERO
                )
                ordinary_recommended = rounded_order_qty(
                    ordinary_raw,
                    min_order_qty=policy.min_order_qty,
                    max_order_qty=policy.max_order_qty,
                    order_rounding_rules=policy.order_rounding_rules,
                )
                min_qty = (
                    max(ordinary_min_qty, acceleration_base_min_qty)
                    + acceleration_open_before
                    + acceleration_allocated
                )
                max_qty = (
                    max(ordinary_max_qty, acceleration_base_max_qty)
                    + acceleration_open_before
                    + acceleration_allocated
                )
                target_qty = max_qty + safety_units
                ordinary_pipeline_qty = max(
                    ZERO,
                    pipeline_qty[code] - acceleration_open_before,
                )
                effective_pipeline_qty = (
                    ordinary_pipeline_qty * min(acceleration_pipeline_share, base_pipeline_share)
                    + acceleration_open_before
                )
                position = stock[code] - reserve + effective_pipeline_qty
                triggered = position <= min_qty
                raw = _ceil(max(ZERO, target_qty - position)) if triggered else ZERO
                recommended_with_acceleration = rounded_order_qty(
                    raw,
                    min_order_qty=policy.min_order_qty,
                    max_order_qty=policy.max_order_qty,
                    order_rounding_rules=policy.order_rounding_rules,
                )
                acceleration_order_component = max(
                    ZERO,
                    recommended_with_acceleration - ordinary_recommended,
                )
                acceleration_order_component = min(
                    acceleration_allocated,
                    acceleration_order_component,
                )
                recommended = ordinary_recommended + acceleration_order_component
            else:
                if scenario.base_pipeline_lot_risk_boundary:
                    effective_pipeline_qty = risk_adjusted_base_pipeline_qty
                else:
                    effective_pipeline_qty = pipeline_qty[code] * min(
                        acceleration_pipeline_share,
                        base_pipeline_share,
                    )
                position = stock[code] - reserve + effective_pipeline_qty
                triggered = position <= min_qty
                raw = _ceil(max(ZERO, target_qty - position)) if triggered else ZERO
                recommended = rounded_order_qty(
                    raw,
                    min_order_qty=policy.min_order_qty,
                    max_order_qty=policy.max_order_qty,
                    order_rounding_rules=policy.order_rounding_rules,
                )
                ordinary_recommended = recommended

            if normalized_hybrid_quantile != "off" and status == AssortmentStatus.SALE.value:
                hybrid_lead_days = int(
                    row.get(f"lead_time_{normalized_hybrid_quantile}_days")
                    or row.get("lead_time_p50_days")
                    or 52
                )
                hybrid_rate = completed_hybrid_demand_rate(
                    sales_by_code.get(code, {}),
                    as_of=cursor,
                    forecast_rate=rate,
                )
                hybrid_evaluation = evaluate_hybrid_coverable_gap(
                    as_of=cursor,
                    demand_rate=hybrid_rate,
                    new_arrival_lead_days=hybrid_lead_days,
                    model_stock_qty=stock[code],
                    effective_reserve_qty=reserve,
                    arrivals=arrivals,
                    code=code,
                    open_hybrid_qty=open_hybrid_protection_qty[code],
                    min_coverable_days=normalized_hybrid_min_days,
                )
                if hybrid_acceleration_filter_enabled:
                    hybrid_acceleration_signal = calculate_demand_acceleration(
                        sales_by_code.get(code, {}),
                        as_of=cursor,
                        recent_days=normalized_hybrid_acceleration_recent_days,
                        baseline_days=normalized_hybrid_acceleration_baseline_days,
                        min_recent_sales=normalized_hybrid_acceleration_min_recent_sales,
                        rate_multiplier=normalized_hybrid_acceleration_rate_multiplier,
                    )
                    hybrid_acceleration_passed = bool(
                        hybrid_acceleration_signal.triggered
                        and (
                            not hybrid_gap_acceleration_require_forecast_growth
                            or hybrid_acceleration_signal.recent_rate > rate
                        )
                    )
                    if hybrid_evaluation.eligible:
                        diagnostics.hybrid_gap_acceleration_evaluations += 1
                        diagnostics.hybrid_gap_acceleration_passed_evaluations += int(
                            hybrid_acceleration_passed
                        )
                        diagnostics.hybrid_gap_acceleration_blocked_evaluations += int(
                            not hybrid_acceleration_passed
                        )
                diagnostics.hybrid_gap_evaluations += 1
                diagnostics.hybrid_gap_eligible_evaluations += int(hybrid_evaluation.eligible)
                diagnostics.hybrid_gap_open_lot_blocked_evaluations += int(
                    hybrid_evaluation.coverable_shortage_qty > ZERO
                    and hybrid_evaluation.open_hybrid_qty > ZERO
                )
                if hybrid_evaluation.eligible and hybrid_acceleration_passed:
                    hybrid_requested = hybrid_evaluation.coverable_shortage_qty
                    diagnostics.hybrid_gap_requested_qty += hybrid_requested
                    hybrid_raw = max(raw, _ceil(hybrid_requested))
                    hybrid_recommended = rounded_order_qty(
                        hybrid_raw,
                        min_order_qty=policy.min_order_qty,
                        max_order_qty=policy.max_order_qty,
                        order_rounding_rules=policy.order_rounding_rules,
                    )
                    hybrid_order_component = max(ZERO, hybrid_recommended - recommended)
                    hybrid_order_component = min(hybrid_requested, hybrid_order_component)
                    if hybrid_order_component > ZERO:
                        recommended += hybrid_order_component
                        diagnostics.hybrid_gap_positive_order_decisions += 1
                        diagnostics.hybrid_gap_order_component_qty += hybrid_order_component
            last_evaluation_by_code[code] = {
                "evaluation_date": cursor.isoformat(),
                "input_decision_date": _clean(row.get("decision_date")),
                "fresh_decision": int(fresh_decision),
                "scheduled_review": int(scheduled_review),
                "forecast_rate_sales": str(rate),
                "selected_lead_time_days": lead_days,
                "simulated_arrival_lead_time_days": arrival_lead_days,
                "lead_time_p75_days": int(row.get("lead_time_p75_days") or lead_days),
                "lead_time_confidence": _clean(row.get("lead_time_confidence")),
                "min_stock_qty": str(min_qty),
                "max_stock_qty": str(max_qty),
                "safety_stock_qty": str(safety_units),
                "decision_service_buffer_qty": str(decision_service_buffer),
                "representation_minimum_qty": str(representation_minimum_qty),
                "target_stock_qty": str(target_qty),
                "model_stock_qty": str(stock[code]),
                "reserve_qty": str(reserve),
                "model_pipeline_qty": str(pipeline_qty[code]),
                "effective_model_pipeline_qty": str(effective_pipeline_qty),
                "base_pipeline_profile": scenario.base_pipeline_profile,
                "base_pipeline_fraction": str(base_pipeline_share),
                "base_pipeline_min_margin_to_cost_ratio": str(
                    scenario.base_pipeline_min_margin_to_cost_ratio
                ),
                "base_pipeline_margin_to_cost_ratio": str(
                    current_margin[code] / current_cost[code] if current_cost[code] > ZERO else ZERO
                ),
                "base_pipeline_lot_risk_boundary": (scenario.base_pipeline_lot_risk_boundary),
                "base_pipeline_lot_risk_boundary_days": (base_pipeline_lot_risk_boundary_days),
                "base_pipeline_lot_risk_fraction": str(scenario.base_pipeline_lot_risk_fraction),
                "base_pipeline_lot_risky_qty": str(base_pipeline_lot_risky_qty),
                "inventory_position_qty": str(position),
                "triggered": int(triggered),
                "recommended_order_qty_raw": str(raw),
                "recommended_order_qty": str(recommended),
                "hybrid_gap_arrival_quantile": normalized_hybrid_quantile,
                "hybrid_gap_requested_qty": str(hybrid_requested),
                "hybrid_gap_order_component_qty": str(hybrid_order_component),
                "expected_arrival_date": (
                    (cursor + timedelta(days=max(1, arrival_lead_days))).isoformat()
                    if recommended > ZERO
                    else ""
                ),
            }
            model[code].ending_target_stock_qty = target_qty
            manual_review_action = ""
            if recommended > ZERO and manual:
                if code in manual_review_seen:
                    manual_review_action = "updated"
                else:
                    manual_review_seen.add(code)
                    manual_review_action = "created"
            if recommended > ZERO:
                arrival = cursor + timedelta(days=max(1, arrival_lead_days))
                ordinary_arrival_qty = max(ZERO, recommended - hybrid_order_component)
                if ordinary_arrival_qty > ZERO:
                    arrivals[arrival][code] += ordinary_arrival_qty
                if hybrid_order_component > ZERO:
                    hybrid_arrival = hybrid_evaluation.new_arrival_date or arrival
                    arrivals[hybrid_arrival][code] += hybrid_order_component
                    hybrid_arrivals[hybrid_arrival][code] += hybrid_order_component
                    open_hybrid_protection_qty[code] += hybrid_order_component
                pipeline_qty[code] += recommended
                if acceleration_order_component > ZERO:
                    acceleration_arrivals[arrival][code] += acceleration_order_component
                    open_acceleration_protection_qty[code] += acceleration_order_component
                    acceleration_open_after = open_acceleration_protection_qty[code]
                    diagnostics.acceleration_order_component_qty += acceleration_order_component
                    diagnostics.acceleration_open_protection_peak_qty = max(
                        diagnostics.acceleration_open_protection_peak_qty,
                        sum(open_acceleration_protection_qty.values(), ZERO),
                    )
                metric = model[code]
                metric.order_qty += recommended
                metric.order_value_rub += recommended * current_cost[code]
                metric.order_lines += 1
                metric.manual_order_lines += int(manual)
                metric.manual_review_created += int(manual_review_action == "created")
                metric.manual_review_updated += int(manual_review_action == "updated")
                metric.safety_stock_units_ordered += safety_units
                stage_metric = model_by_stage[status]
                stage_metric.order_qty += recommended
                stage_metric.order_value_rub += recommended * current_cost[code]
                stage_metric.order_lines += 1
                stage_metric.manual_order_lines += int(manual)
                stage_metric.manual_review_created += int(manual_review_action == "created")
                stage_metric.manual_review_updated += int(manual_review_action == "updated")
                stage_metric.safety_stock_units_ordered += safety_units
            if (
                keep_detail
                and keep_decision_detail
                and (
                    scheduled_review
                    or recommended > ZERO
                    or (fresh_decision and (rate > ZERO or weighted_signals > ZERO))
                )
                and (
                    not hybrid_gap_detail_only
                    or hybrid_evaluation.coverable_shortage_qty > ZERO
                    or hybrid_order_component > ZERO
                )
                and (
                    not acceleration_detail_only
                    or acceleration_stock_above_min_passed
                    and (acceleration_signal.triggered or acceleration_order_component > ZERO)
                )
            ):
                trigger = (
                    "scheduled_review"
                    if scheduled_review
                    else (
                        "acceleration_review"
                        if acceleration_allocated > ZERO
                        else "event_review" if fresh_decision else "stockout_guard"
                    )
                )
                decision_detail.append(
                    {
                        "scenario_id": scenario.scenario_id,
                        "decision_date": cursor.isoformat(),
                        "nomenclature_code": code,
                        "status": status,
                        "decision_trigger": trigger,
                        "forecast_rate_sales": str(rate),
                        "kmp4_open_weighted_qty": str(weighted_kmp),
                        "site_order_open_weighted_qty": str(weighted_site_orders),
                        "site_cart_open_weighted_qty": str(weighted_site_carts),
                        "reserve_backlog_qty": str(_decimal(fact.get("reserve_backlog_qty"))),
                        "selected_lead_time_days": lead_days,
                        "simulated_arrival_lead_time_days": arrival_lead_days,
                        "lead_time_confidence": _clean(row.get("lead_time_confidence")),
                        "unprotected_min_stock_qty": str(unprotected_min_qty),
                        "unprotected_max_stock_qty": str(unprotected_max_qty),
                        "min_stock_qty": str(min_qty),
                        "max_stock_qty": str(max_qty),
                        "grow_protection_reason": grow_protection_reason,
                        "grow_weekly_reduction_cap": str(scenario.grow_weekly_reduction_cap),
                        "grow_entry_protection_weeks": (scenario.grow_entry_protection_weeks),
                        "forecast_error_percentile": str(scenario.forecast_error_percentile),
                        "forecast_error_percentile_qty": str(percentile_safety_target),
                        "economic_safety_cap_qty": str(economic_safety_cap),
                        "economic_safety_stock_qty": str(safety_units),
                        "decision_service_buffer_qty": str(decision_service_buffer),
                        "representation_minimum_qty": str(representation_minimum_qty),
                        "service_floor_percentile": str(scenario.grow_service_floor_percentile),
                        "service_floor_requested_qty": str(service_floor_requested),
                        "service_floor_sku_capped_qty": str(service_floor_sku_capped),
                        "service_floor_allocated_qty": str(service_floor_allocated),
                        "service_floor_unfunded_qty": str(
                            max(ZERO, service_floor_requested - service_floor_allocated)
                        ),
                        "service_floor_effective_unfunded_qty": str(
                            max(ZERO, service_floor_requested - safety_units)
                        ),
                        "service_floor_sku_cap_rub": str(scenario.grow_service_floor_sku_cap_rub),
                        "service_floor_stage_budget_rub": str(
                            scenario.grow_service_floor_stage_budget_rub
                        ),
                        "service_floor_budget_limited": int(
                            service_floor_allocated < service_floor_requested
                        ),
                        "acceleration_profile": scenario.grow_acceleration_profile,
                        "acceleration_quantity_policy": (
                            scenario.grow_acceleration_quantity_policy
                        ),
                        "acceleration_recent_days": scenario.grow_acceleration_recent_days,
                        "acceleration_baseline_days": (scenario.grow_acceleration_baseline_days),
                        "acceleration_min_recent_sales": str(
                            scenario.grow_acceleration_min_recent_sales
                        ),
                        "acceleration_rate_multiplier": str(
                            scenario.grow_acceleration_rate_multiplier
                        ),
                        "acceleration_recent_sales_qty": str(acceleration_signal.recent_qty),
                        "acceleration_baseline_sales_qty": str(acceleration_signal.baseline_qty),
                        "acceleration_recent_rate": str(acceleration_signal.recent_rate),
                        "acceleration_baseline_rate": str(acceleration_signal.baseline_rate),
                        "acceleration_actual_ratio": str(acceleration_signal.rate_ratio),
                        "acceleration_triggered": int(acceleration_signal.triggered),
                        "acceleration_require_forecast_growth": int(
                            scenario.grow_acceleration_require_forecast_growth
                        ),
                        "acceleration_forecast_growth_passed": int(
                            acceleration_guard.forecast_growth_passed
                        ),
                        "acceleration_min_shortage_qty": str(
                            scenario.grow_acceleration_min_shortage_qty
                        ),
                        "acceleration_projected_demand_to_p75_qty": str(
                            acceleration_guard.projected_demand_qty
                        ),
                        "acceleration_guard_inventory_position_qty": str(
                            acceleration_guard.inventory_position_qty
                        ),
                        "acceleration_projected_shortage_to_p75_qty": str(
                            acceleration_guard.projected_shortage_qty
                        ),
                        "acceleration_gross_projected_shortage_to_p75_qty": str(
                            acceleration_guard.gross_projected_shortage_qty
                        ),
                        "acceleration_shortage_passed": int(acceleration_guard.shortage_passed),
                        "acceleration_guard_eligible": int(acceleration_guard.eligible),
                        "acceleration_require_stock_above_min": int(
                            acceleration_require_stock_above_min
                        ),
                        "acceleration_lead_quantile": normalized_acceleration_lead_quantile,
                        "acceleration_static_min_stock_qty": str(acceleration_static_min_qty),
                        "acceleration_free_stock_qty": str(acceleration_free_stock_qty),
                        "acceleration_stock_above_min_passed": int(
                            acceleration_stock_above_min_passed
                        ),
                        "acceleration_cap_to_projected_shortage": int(
                            scenario.grow_acceleration_cap_to_projected_shortage
                        ),
                        "acceleration_uncapped_requested_qty": str(acceleration_uncapped_requested),
                        "acceleration_shortage_cap_reduction_qty": str(
                            max(
                                ZERO,
                                acceleration_uncapped_requested - acceleration_requested,
                            )
                        ),
                        "acceleration_requested_qty": str(acceleration_requested),
                        "acceleration_sku_capped_qty": str(acceleration_sku_capped),
                        "acceleration_allocated_qty": str(acceleration_allocated),
                        "acceleration_single_open_lot": int(
                            scenario.grow_acceleration_single_open_lot
                        ),
                        "acceleration_segment_profile": (
                            scenario.grow_acceleration_segment_profile
                        ),
                        "acceleration_demand_pattern_preperiod": (
                            acceleration_segment_gate.demand_pattern
                        ),
                        "acceleration_segment_pattern_passed": int(
                            acceleration_segment_gate.pattern_passed
                        ),
                        "acceleration_segment_cost_passed": int(
                            acceleration_segment_gate.cost_passed
                        ),
                        "acceleration_segment_confidence_passed": int(
                            acceleration_segment_gate.confidence_passed
                        ),
                        "acceleration_segment_p75_passed": int(
                            acceleration_segment_gate.p75_passed
                        ),
                        "acceleration_segment_gate_eligible": int(
                            acceleration_segment_gate.eligible
                        ),
                        "acceleration_open_protection_before_qty": str(acceleration_open_before),
                        "acceleration_open_protection_after_qty": str(acceleration_open_after),
                        "acceleration_released_on_arrival_qty": str(
                            released_acceleration_today[code]
                        ),
                        "acceleration_order_component_qty": str(acceleration_order_component),
                        "acceleration_unfunded_qty": str(
                            max(ZERO, acceleration_requested - acceleration_allocated)
                        ),
                        "acceleration_sku_cap_rub": str(scenario.grow_acceleration_sku_cap_rub),
                        "acceleration_stage_budget_rub": str(
                            scenario.grow_acceleration_stage_budget_rub
                        ),
                        "acceleration_budget_limited": int(
                            acceleration_allocated < acceleration_requested
                        ),
                        "acceleration_pipeline_fraction": str(acceleration_pipeline_share),
                        "base_pipeline_profile": scenario.base_pipeline_profile,
                        "base_pipeline_fraction": str(base_pipeline_share),
                        "base_pipeline_min_margin_to_cost_ratio": str(
                            scenario.base_pipeline_min_margin_to_cost_ratio
                        ),
                        "base_pipeline_margin_to_cost_ratio": str(
                            current_margin[code] / current_cost[code]
                            if current_cost[code] > ZERO
                            else ZERO
                        ),
                        "base_pipeline_lot_risk_boundary": (
                            scenario.base_pipeline_lot_risk_boundary
                        ),
                        "base_pipeline_lot_risk_boundary_days": (
                            base_pipeline_lot_risk_boundary_days
                        ),
                        "base_pipeline_lot_risk_fraction": str(
                            scenario.base_pipeline_lot_risk_fraction
                        ),
                        "base_pipeline_lot_risky_qty": str(base_pipeline_lot_risky_qty),
                        "model_stock_qty": str(stock[code]),
                        "reserve_qty": str(reserve),
                        "model_pipeline_qty": str(pipeline_qty[code]),
                        "effective_model_pipeline_qty": str(effective_pipeline_qty),
                        "inventory_position_qty": str(position),
                        "ordinary_min_stock_qty": str(ordinary_min_qty),
                        "ordinary_max_stock_qty": str(ordinary_max_qty),
                        "ordinary_recommended_order_qty": str(ordinary_recommended),
                        "hybrid_gap_arrival_quantile": normalized_hybrid_quantile,
                        "hybrid_gap_min_coverable_days": normalized_hybrid_min_days,
                        "hybrid_gap_acceleration_filter_enabled": int(
                            hybrid_acceleration_filter_enabled
                        ),
                        "hybrid_gap_acceleration_recent_days": (
                            normalized_hybrid_acceleration_recent_days
                        ),
                        "hybrid_gap_acceleration_baseline_days": (
                            normalized_hybrid_acceleration_baseline_days
                        ),
                        "hybrid_gap_acceleration_min_recent_sales": str(
                            normalized_hybrid_acceleration_min_recent_sales
                        ),
                        "hybrid_gap_acceleration_rate_multiplier": str(
                            normalized_hybrid_acceleration_rate_multiplier
                        ),
                        "hybrid_gap_acceleration_require_forecast_growth": int(
                            hybrid_gap_acceleration_require_forecast_growth
                        ),
                        "hybrid_gap_acceleration_recent_sales_qty": str(
                            hybrid_acceleration_signal.recent_qty
                        ),
                        "hybrid_gap_acceleration_baseline_sales_qty": str(
                            hybrid_acceleration_signal.baseline_qty
                        ),
                        "hybrid_gap_acceleration_recent_rate": str(
                            hybrid_acceleration_signal.recent_rate
                        ),
                        "hybrid_gap_acceleration_baseline_rate": str(
                            hybrid_acceleration_signal.baseline_rate
                        ),
                        "hybrid_gap_acceleration_rate_ratio": str(
                            hybrid_acceleration_signal.rate_ratio
                        ),
                        "hybrid_gap_acceleration_triggered": int(
                            hybrid_acceleration_signal.triggered
                        ),
                        "hybrid_gap_acceleration_passed": int(hybrid_acceleration_passed),
                        "hybrid_gap_demand_rate": str(hybrid_evaluation.demand_rate),
                        "hybrid_gap_new_arrival_date": (
                            hybrid_evaluation.new_arrival_date.isoformat()
                            if hybrid_evaluation.new_arrival_date
                            else ""
                        ),
                        "hybrid_gap_reliable_arrival_date": (
                            hybrid_evaluation.reliable_arrival_date.isoformat()
                            if hybrid_evaluation.reliable_arrival_date
                            else ""
                        ),
                        "hybrid_gap_coverable_days": hybrid_evaluation.coverable_days,
                        "hybrid_gap_stock_at_new_arrival_qty": str(
                            hybrid_evaluation.stock_at_new_arrival_qty
                        ),
                        "hybrid_gap_coverable_demand_qty": str(
                            hybrid_evaluation.coverable_demand_qty
                        ),
                        "hybrid_gap_coverable_shortage_qty": str(
                            hybrid_evaluation.coverable_shortage_qty
                        ),
                        "hybrid_gap_open_before_qty": str(hybrid_evaluation.open_hybrid_qty),
                        "hybrid_gap_eligible": int(hybrid_evaluation.eligible),
                        "hybrid_gap_requested_qty": str(hybrid_requested),
                        "hybrid_gap_order_component_qty": str(hybrid_order_component),
                        "recommended_order_qty_raw": str(raw),
                        "recommended_order_qty": str(recommended),
                        "manual_review_assumed_accepted": int(manual),
                        "manual_review_action": manual_review_action,
                    }
                )
        if keep_detail:
            daily_detail.append(
                {
                    key: str(value) if isinstance(value, Decimal) else value
                    for key, value in daily.items()
                }
            )
        cursor += timedelta(days=1)

    diagnostics.acceleration_open_protection_ending_qty = sum(
        open_acceleration_protection_qty.values(),
        ZERO,
    )
    return SimulationResult(
        scenario=scenario,
        actual=actual,
        model=model,
        actual_by_stage=dict(actual_by_stage),
        model_by_stage=dict(model_by_stage),
        decision_rows=decision_detail,
        daily_rows=daily_detail,
        loss_rows=loss_detail,
        diagnostics=diagnostics,
    )


def _sku_comparison_rows(result: SimulationResult, period_days: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for code in sorted(result.model):
        actual = result.actual[code]
        model = result.model[code]
        rows.append(
            {
                "nomenclature_code": code,
                "actual_served_qty": str(actual.served_qty),
                "actual_served_observed_qty": str(actual.served_observed_qty),
                "actual_served_hidden_qty": str(actual.served_hidden_qty),
                "actual_hidden_kmp4_qty": str(actual.hidden_kmp4_qty),
                "actual_hidden_site_order_qty": str(actual.hidden_site_order_qty),
                "actual_hidden_site_cart_qty": str(actual.hidden_site_cart_qty),
                "actual_hidden_reserve_backlog_qty": str(actual.hidden_reserve_backlog_qty),
                "model_served_qty": str(model.served_qty),
                "model_served_observed_qty": str(model.served_observed_qty),
                "model_served_hidden_qty": str(model.served_hidden_qty),
                "model_served_hidden_site_order_qty": str(model.served_hidden_site_order_qty),
                "model_served_hidden_site_cart_qty": str(model.served_hidden_site_cart_qty),
                "model_served_hidden_reserve_backlog_qty": str(
                    model.served_hidden_reserve_backlog_qty
                ),
                "served_delta_qty": str(model.served_qty - actual.served_qty),
                "actual_lost_qty": str(actual.lost_qty),
                "actual_lost_observed_qty": str(actual.lost_observed_qty),
                "actual_lost_hidden_qty": str(actual.lost_hidden_qty),
                "model_lost_qty": str(model.lost_qty),
                "model_lost_observed_qty": str(model.lost_observed_qty),
                "model_lost_hidden_qty": str(model.lost_hidden_qty),
                "actual_gross_profit_rub": str(actual.gross_profit_rub),
                "model_gross_profit_rub": str(model.gross_profit_rub),
                "gross_profit_delta_rub": str(model.gross_profit_rub - actual.gross_profit_rub),
                "actual_average_inventory_value_rub": str(
                    actual.inventory_value_days_rub / Decimal(period_days)
                ),
                "model_average_inventory_value_rub": str(
                    model.inventory_value_days_rub / Decimal(period_days)
                ),
                "capital_delta_rub": str(
                    (model.inventory_value_days_rub - actual.inventory_value_days_rub)
                    / Decimal(period_days)
                ),
                "model_order_qty": str(model.order_qty),
                "model_order_value_rub": str(model.order_value_rub),
                "manual_order_lines": model.manual_order_lines,
                "manual_review_created": model.manual_review_created,
                "manual_review_updated": model.manual_review_updated,
                "exogenous_launch_seed_qty": str(model.exogenous_launch_seed_qty),
            }
        )
    rows.sort(key=lambda row: abs(_decimal(row["gross_profit_delta_rub"])), reverse=True)
    return rows


def _stage_summary_rows(result: SimulationResult, period_days: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    stages = sorted(set(result.actual_by_stage) | set(result.model_by_stage))
    for stage in stages:
        for strategy, metrics in (
            ("actual", result.actual_by_stage),
            ("model", result.model_by_stage),
        ):
            metric = metrics.get(stage, Metric())
            potential = metric.potential_demand_qty
            average_inventory = metric.inventory_value_days_rub / Decimal(period_days)
            rows.append(
                {
                    "scenario_id": result.scenario.scenario_id,
                    "strategy": strategy,
                    "status": stage,
                    "potential_demand_qty": str(potential),
                    "observed_demand_qty": str(metric.observed_demand_qty),
                    "hidden_demand_qty": str(metric.hidden_demand_qty),
                    "hidden_kmp4_qty": str(metric.hidden_kmp4_qty),
                    "hidden_site_order_qty": str(metric.hidden_site_order_qty),
                    "hidden_site_cart_qty": str(metric.hidden_site_cart_qty),
                    "hidden_reserve_backlog_qty": str(metric.hidden_reserve_backlog_qty),
                    "served_qty": str(metric.served_qty),
                    "served_observed_qty": str(metric.served_observed_qty),
                    "served_hidden_qty": str(metric.served_hidden_qty),
                    "served_hidden_site_order_qty": str(metric.served_hidden_site_order_qty),
                    "served_hidden_site_cart_qty": str(metric.served_hidden_site_cart_qty),
                    "served_hidden_reserve_backlog_qty": str(
                        metric.served_hidden_reserve_backlog_qty
                    ),
                    "lost_qty": str(metric.lost_qty),
                    "lost_observed_qty": str(metric.lost_observed_qty),
                    "lost_hidden_qty": str(metric.lost_hidden_qty),
                    "fill_rate": str(metric.served_qty / potential if potential > ZERO else ONE),
                    "observed_fill_rate": str(
                        metric.served_observed_qty / metric.observed_demand_qty
                        if metric.observed_demand_qty > ZERO
                        else ONE
                    ),
                    "hidden_fill_rate": str(
                        metric.served_hidden_qty / metric.hidden_demand_qty
                        if metric.hidden_demand_qty > ZERO
                        else ONE
                    ),
                    "gross_profit_rub": str(metric.gross_profit_rub),
                    "average_inventory_value_rub": str(average_inventory),
                    "order_qty": str(metric.order_qty),
                    "order_value_rub": str(metric.order_value_rub),
                    "order_lines": metric.order_lines,
                    "manual_order_lines": metric.manual_order_lines,
                    "manual_review_created": metric.manual_review_created,
                    "manual_review_updated": metric.manual_review_updated,
                    "economic_safety_stock_qty": str(metric.safety_stock_units_ordered),
                    "exogenous_launch_seed_qty": str(metric.exogenous_launch_seed_qty),
                    "inventory_valuation_coverage": str(
                        metric.priced_inventory_qty_days / metric.inventory_qty_days
                        if metric.inventory_qty_days > ZERO
                        else ONE
                    ),
                }
            )
    return rows


def _period_summary_rows(
    daily_rows: Sequence[Mapping[str, Any]],
    *,
    scenario_id: str,
    date_from: date,
    date_to: date,
) -> list[dict[str, Any]]:
    periods = (
        ("pre_july", date_from, min(date_to, date(2026, 6, 30))),
        ("july", max(date_from, date(2026, 7, 1)), date_to),
    )
    rows: list[dict[str, Any]] = []
    source_rows = [row for row in daily_rows if _clean(row.get("scenario_id")) == scenario_id]
    for period_name, period_from, period_to in periods:
        if period_from > period_to:
            continue
        selected = [
            row
            for row in source_rows
            if (business_date := _date(row.get("business_date"))) is not None
            and period_from <= business_date <= period_to
        ]
        days = (period_to - period_from).days + 1
        for strategy in ("actual", "model"):
            observed = sum(
                (_decimal(row.get("actual_observed_demand_qty")) for row in selected), ZERO
            )
            hidden = sum((_decimal(row.get("actual_hidden_demand_qty")) for row in selected), ZERO)
            served = sum((_decimal(row.get(f"{strategy}_served_qty")) for row in selected), ZERO)
            served_observed = sum(
                (_decimal(row.get(f"{strategy}_served_observed_qty")) for row in selected),
                ZERO,
            )
            potential = observed + hidden
            gross_profit = sum(
                (_decimal(row.get(f"{strategy}_gross_profit_rub")) for row in selected),
                ZERO,
            )
            average_inventory = sum(
                (_decimal(row.get(f"{strategy}_inventory_value_rub")) for row in selected),
                ZERO,
            ) / Decimal(days)
            rows.append(
                {
                    "scenario_id": scenario_id,
                    "period": period_name,
                    "date_from": period_from.isoformat(),
                    "date_to": period_to.isoformat(),
                    "strategy": strategy,
                    "observed_demand_qty": str(observed),
                    "hidden_demand_qty": str(hidden),
                    "potential_demand_qty": str(potential),
                    "served_qty": str(served),
                    "served_observed_qty": str(served_observed),
                    "lost_qty": str(potential - served),
                    "lost_observed_qty": str(observed - served_observed),
                    "fill_rate": str(served / potential if potential > ZERO else ONE),
                    "observed_fill_rate": str(
                        served_observed / observed if observed > ZERO else ONE
                    ),
                    "gross_profit_rub": str(gross_profit),
                    "average_inventory_value_rub": str(average_inventory),
                }
            )
    return rows


def _quick_comparison_rows(
    summary_rows: Sequence[Mapping[str, Any]],
    *,
    scenario_roles: Mapping[str, str],
) -> list[dict[str, Any]]:
    model_by_id = {
        _clean(row.get("scenario_id")): row
        for row in summary_rows
        if _clean(row.get("strategy")) == "model"
    }
    control_id = scenario_roles["control"]
    control = model_by_id[control_id]
    rows: list[dict[str, Any]] = []
    for role in ("control", "hypothesis", "cautious"):
        scenario_id = scenario_roles[role]
        row = model_by_id[scenario_id]
        rows.append(
            {
                "scenario_role": role,
                "scenario_id": scenario_id,
                "served_observed_qty": row["served_observed_qty"],
                "served_observed_delta_to_control_qty": str(
                    _decimal(row["served_observed_qty"]) - _decimal(control["served_observed_qty"])
                ),
                "observed_fill_rate": row["observed_fill_rate"],
                "observed_fill_rate_delta_to_control": str(
                    _decimal(row["observed_fill_rate"]) - _decimal(control["observed_fill_rate"])
                ),
                "gross_profit_rub": row["gross_profit_rub"],
                "gross_profit_delta_to_control_rub": str(
                    _decimal(row["gross_profit_rub"]) - _decimal(control["gross_profit_rub"])
                ),
                "average_inventory_value_rub": row["average_inventory_value_rub"],
                "capital_delta_to_control_rub": str(
                    _decimal(row["average_inventory_value_rub"])
                    - _decimal(control["average_inventory_value_rub"])
                ),
                "ending_inventory_qty": row["ending_inventory_qty"],
                "ending_inventory_delta_to_control_qty": str(
                    _decimal(row["ending_inventory_qty"])
                    - _decimal(control["ending_inventory_qty"])
                ),
                "economic_contribution_rub": row["economic_contribution_rub"],
                "economic_contribution_delta_to_control_rub": str(
                    _decimal(row["economic_contribution_rub"])
                    - _decimal(control["economic_contribution_rub"])
                ),
                "order_value_rub": row["order_value_rub"],
                "manual_review_created": row["manual_review_created"],
                "acceleration_require_forecast_growth": row.get(
                    "grow_acceleration_require_forecast_growth", 0
                ),
                "acceleration_min_shortage_qty": row.get("grow_acceleration_min_shortage_qty", "0"),
                "acceleration_cap_to_projected_shortage": row.get(
                    "grow_acceleration_cap_to_projected_shortage", 0
                ),
                "acceleration_single_open_lot": row.get("grow_acceleration_single_open_lot", 0),
                "acceleration_segment_profile": row.get(
                    "grow_acceleration_segment_profile",
                    ACCELERATION_SEGMENT_PROFILE_OFF,
                ),
                "acceleration_allowed_demand_patterns": row.get(
                    "grow_acceleration_allowed_demand_patterns", ""
                ),
                "acceleration_max_unit_cost_rub": row.get(
                    "grow_acceleration_max_unit_cost_rub", "0"
                ),
                "acceleration_allowed_lead_confidences": row.get(
                    "grow_acceleration_allowed_lead_confidences", ""
                ),
                "acceleration_max_p75_days": row.get("grow_acceleration_max_p75_days", 0),
                "base_pipeline_profile": row.get(
                    "base_pipeline_profile", BASE_PIPELINE_PROFILE_OFF
                ),
                "base_pipeline_high_fraction": row.get("base_pipeline_high_fraction", "1"),
                "base_pipeline_medium_fraction": row.get("base_pipeline_medium_fraction", "1"),
                "base_pipeline_low_fraction": row.get("base_pipeline_low_fraction", "1"),
                "base_pipeline_min_margin_to_cost_ratio": row.get(
                    "base_pipeline_min_margin_to_cost_ratio", "0"
                ),
                "base_pipeline_lot_risk_boundary": row.get("base_pipeline_lot_risk_boundary", ""),
                "base_pipeline_lot_risk_fraction": row.get("base_pipeline_lot_risk_fraction", "1"),
                "base_pipeline_lot_risk_evaluations": row.get(
                    "base_pipeline_lot_risk_evaluations", 0
                ),
                "base_pipeline_lot_risk_positive_evaluations": row.get(
                    "base_pipeline_lot_risk_positive_evaluations", 0
                ),
                "base_pipeline_lot_risk_qty_evaluated": row.get(
                    "base_pipeline_lot_risk_qty_evaluated", "0"
                ),
                "base_pipeline_lot_risk_effective_reduction_qty": row.get(
                    "base_pipeline_lot_risk_effective_reduction_qty", "0"
                ),
                "acceleration_triggered_recalculations": row.get(
                    "acceleration_triggered_recalculations", 0
                ),
                "acceleration_forecast_growth_passed_recalculations": row.get(
                    "acceleration_forecast_growth_passed_recalculations", 0
                ),
                "acceleration_shortage_passed_recalculations": row.get(
                    "acceleration_shortage_passed_recalculations", 0
                ),
                "acceleration_guard_eligible_recalculations": row.get(
                    "acceleration_guard_eligible_recalculations", 0
                ),
                "acceleration_positive_recalculations": row.get(
                    "acceleration_positive_recalculations", 0
                ),
                "acceleration_shortage_capped_recalculations": row.get(
                    "acceleration_shortage_capped_recalculations", 0
                ),
                "acceleration_budget_limited_recalculations": row.get(
                    "acceleration_budget_limited_recalculations", 0
                ),
                "acceleration_uncapped_requested_qty": row.get(
                    "acceleration_uncapped_requested_qty", "0"
                ),
                "acceleration_shortage_cap_reduction_qty": row.get(
                    "acceleration_shortage_cap_reduction_qty", "0"
                ),
                "acceleration_requested_qty": row.get("acceleration_requested_qty", "0"),
                "acceleration_allocated_qty": row.get("acceleration_allocated_qty", "0"),
                "acceleration_single_open_blocked_recalculations": row.get(
                    "acceleration_single_open_blocked_recalculations", 0
                ),
                "acceleration_single_open_blocked_qty": row.get(
                    "acceleration_single_open_blocked_qty", "0"
                ),
                "acceleration_order_component_qty": row.get(
                    "acceleration_order_component_qty", "0"
                ),
                "acceleration_released_on_arrival_qty": row.get(
                    "acceleration_released_on_arrival_qty", "0"
                ),
                "acceleration_open_protection_peak_qty": row.get(
                    "acceleration_open_protection_peak_qty", "0"
                ),
                "acceleration_open_protection_ending_qty": row.get(
                    "acceleration_open_protection_ending_qty", "0"
                ),
                "acceleration_segment_evaluated_recalculations": row.get(
                    "acceleration_segment_evaluated_recalculations", 0
                ),
                "acceleration_segment_passed_recalculations": row.get(
                    "acceleration_segment_passed_recalculations", 0
                ),
                "acceleration_segment_blocked_recalculations": row.get(
                    "acceleration_segment_blocked_recalculations", 0
                ),
                "acceleration_segment_blocked_pattern_recalculations": row.get(
                    "acceleration_segment_blocked_pattern_recalculations", 0
                ),
                "acceleration_segment_blocked_cost_recalculations": row.get(
                    "acceleration_segment_blocked_cost_recalculations", 0
                ),
                "acceleration_segment_blocked_confidence_recalculations": row.get(
                    "acceleration_segment_blocked_confidence_recalculations", 0
                ),
                "acceleration_segment_blocked_p75_recalculations": row.get(
                    "acceleration_segment_blocked_p75_recalculations", 0
                ),
                "acceptance_passed": row["acceptance_passed"],
                "diagnostic_only": 1,
            }
        )
    return rows


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--auto-order-policy-json",
        type=Path,
        default=Path("config/assortment/display-auto-order-policy.json"),
    )
    parser.add_argument(
        "--scenario-config-json",
        type=Path,
        default=Path("config/assortment/display-auto-order-backtest-scenarios.json"),
    )
    parser.add_argument(
        "--run-mode",
        choices=(RUN_MODE_FULL, RUN_MODE_QUICK),
        default=RUN_MODE_FULL,
    )
    parser.add_argument("--control-scenario-id")
    parser.add_argument("--hypothesis-scenario-id")
    parser.add_argument("--cautious-scenario-id")
    parser.add_argument(
        "--quick-acceleration-guard",
        action="store_true",
        help=(
            "Require recent growth above forecast and projected P75 shortage for "
            "hypothesis/cautious quick scenarios"
        ),
    )
    parser.add_argument(
        "--hypothesis-min-shortage-qty",
        type=Decimal,
        default=Decimal("2"),
    )
    parser.add_argument(
        "--cautious-min-shortage-qty",
        type=Decimal,
        default=Decimal("3"),
    )
    parser.add_argument(
        "--quick-cap-acceleration-to-shortage",
        action="store_true",
        help="Cap acceleration add-on by the projected shortage through P75",
    )
    parser.add_argument(
        "--quick-single-open-acceleration-lot",
        action="store_true",
        help=(
            "Treat already ordered acceleration protection as one open SKU lot and "
            "order only the remaining shortage growth"
        ),
    )
    parser.add_argument(
        "--quick-hypothesis-acceleration-segment-profile",
        choices=ACCELERATION_SEGMENT_PROFILES,
        default=ACCELERATION_SEGMENT_PROFILE_OFF,
    )
    parser.add_argument(
        "--quick-cautious-acceleration-segment-profile",
        choices=ACCELERATION_SEGMENT_PROFILES,
        default=ACCELERATION_SEGMENT_PROFILE_OFF,
    )
    parser.add_argument(
        "--quick-hypothesis-base-pipeline-profile",
        choices=BASE_PIPELINE_PROFILES,
        default=BASE_PIPELINE_PROFILE_OFF,
    )
    parser.add_argument(
        "--quick-cautious-base-pipeline-profile",
        choices=BASE_PIPELINE_PROFILES,
        default=BASE_PIPELINE_PROFILE_OFF,
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    manifest = validate_preflight_directory(args.preflight_dir)
    date_from = date.fromisoformat(manifest["date_from"])
    date_to = date.fromisoformat(manifest["date_to"])
    period_days = (date_to - date_from).days + 1
    policy = load_auto_order_policy(args.auto_order_policy_json)
    config = load_scenario_config(args.scenario_config_json)

    fact_rows_by_date: dict[date, list[dict[str, str]]] = defaultdict(list)
    sales_by_code: dict[str, dict[date, Decimal]] = defaultdict(dict)
    for row in _read_csv(args.preflight_dir / "historical-sales.csv"):
        business_date = _date(row.get("business_date"))
        code = _clean(row.get("nomenclature_code"))
        if business_date is not None and code:
            sales_by_code[code][business_date] = max(
                ZERO,
                _decimal(row.get("observed_sales_qty")),
            )
    for row in _read_csv(args.preflight_dir / "daily-facts.csv"):
        business_date = _date(row.get("business_date"))
        code = _clean(row.get("nomenclature_code"))
        if business_date is None or not code:
            continue
        fact_rows_by_date[business_date].append(row)
        sales_by_code[code][business_date] = _decimal(row.get("observed_sales_qty"))
    decision_rows_by_date: dict[date, list[dict[str, str]]] = defaultdict(list)
    for row in _read_csv(args.preflight_dir / "decision-inputs.csv"):
        business_date = _date(row.get("decision_date"))
        if business_date is not None:
            decision_rows_by_date[business_date].append(row)
    initial_pipeline = _read_csv(args.preflight_dir / "initial-pipeline.csv")
    frozen_scenarios = _load_scenarios(args.preflight_dir / "scenario-decisions.csv")
    if not frozen_scenarios:
        raise SystemExit("frozen scenario definitions are empty")
    try:
        selection = select_scenarios(
            frozen_scenarios,
            run_mode=args.run_mode,
            control_scenario_id=args.control_scenario_id,
            hypothesis_scenario_id=args.hypothesis_scenario_id,
            cautious_scenario_id=args.cautious_scenario_id,
        )
        source_scenario_roles = dict(selection.scenario_roles)
        if args.quick_cap_acceleration_to_shortage and not args.quick_acceleration_guard:
            raise ValueError("shortage cap requires --quick-acceleration-guard")
        if args.quick_single_open_acceleration_lot and not (
            args.quick_acceleration_guard and args.quick_cap_acceleration_to_shortage
        ):
            raise ValueError(
                "single open acceleration lot requires --quick-acceleration-guard "
                "and --quick-cap-acceleration-to-shortage"
            )
        segment_profiles_enabled = any(
            profile != ACCELERATION_SEGMENT_PROFILE_OFF
            for profile in (
                args.quick_hypothesis_acceleration_segment_profile,
                args.quick_cautious_acceleration_segment_profile,
            )
        )
        if segment_profiles_enabled and not args.quick_single_open_acceleration_lot:
            raise ValueError(
                "acceleration segment profiles require " "--quick-single-open-acceleration-lot"
            )
        base_pipeline_profiles_enabled = any(
            profile != BASE_PIPELINE_PROFILE_OFF
            for profile in (
                args.quick_hypothesis_base_pipeline_profile,
                args.quick_cautious_base_pipeline_profile,
            )
        )
        if base_pipeline_profiles_enabled and (
            args.quick_acceleration_guard
            or args.quick_cap_acceleration_to_shortage
            or args.quick_single_open_acceleration_lot
            or segment_profiles_enabled
        ):
            raise ValueError(
                "base pipeline quick profiles cannot be combined with acceleration overlays"
            )
        if base_pipeline_profiles_enabled and BASE_PIPELINE_PROFILE_OFF in {
            args.quick_hypothesis_base_pipeline_profile,
            args.quick_cautious_base_pipeline_profile,
        }:
            raise ValueError("both base pipeline quick profiles must be selected")
        if args.quick_acceleration_guard:
            selection = apply_quick_acceleration_guard(
                selection,
                hypothesis_min_shortage_qty=args.hypothesis_min_shortage_qty,
                cautious_min_shortage_qty=args.cautious_min_shortage_qty,
                cap_to_projected_shortage=(args.quick_cap_acceleration_to_shortage),
                single_open_lot=(args.quick_single_open_acceleration_lot),
            )
        if segment_profiles_enabled:
            selection = apply_quick_acceleration_segment_gates(
                selection,
                hypothesis_profile=(args.quick_hypothesis_acceleration_segment_profile),
                cautious_profile=(args.quick_cautious_acceleration_segment_profile),
            )
        if base_pipeline_profiles_enabled:
            selection = apply_quick_base_pipeline_profiles(
                selection,
                hypothesis_profile=args.quick_hypothesis_base_pipeline_profile,
                cautious_profile=args.quick_cautious_base_pipeline_profile,
            )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    scenarios = selection.scenarios

    summary_rows: list[dict[str, Any]] = []
    base_result: SimulationResult | None = None
    detail_rows: list[dict[str, Any]] = []
    daily_rows: list[dict[str, Any]] = []
    stage_rows: list[dict[str, Any]] = []
    shared_demand_sample_cache: dict[tuple[str, date, int], list[Decimal]] = {}
    for scenario in scenarios:
        keep_detail = selection.run_mode == RUN_MODE_FULL and scenario.scenario_id in {
            "legacy",
            selection.control_scenario_id,
            selection.base_scenario_id,
        }
        result = simulate_scenario(
            scenario=scenario,
            fact_rows_by_date=fact_rows_by_date,
            decision_rows_by_date=decision_rows_by_date,
            initial_pipeline_rows=initial_pipeline,
            sales_by_code=sales_by_code,
            policy=policy,
            config=config,
            date_from=date_from,
            date_to=date_to,
            keep_detail=keep_detail,
            demand_sample_cache=shared_demand_sample_cache,
        )
        actual_summary = _summary(
            scenario=scenario,
            strategy="actual",
            metrics=result.actual,
            period_days=period_days,
        )
        model_summary = _summary(
            scenario=scenario,
            strategy="model",
            metrics=result.model,
            period_days=period_days,
        )
        model_summary["gross_profit_delta_rub"] = str(
            _decimal(model_summary["gross_profit_rub"])
            - _decimal(actual_summary["gross_profit_rub"])
        )
        model_summary["capital_delta_rub"] = str(
            _decimal(model_summary["average_inventory_value_rub"])
            - _decimal(actual_summary["average_inventory_value_rub"])
        )
        model_summary["fill_rate_delta"] = str(
            _decimal(model_summary["fill_rate"]) - _decimal(actual_summary["fill_rate"])
        )
        model_summary["economic_contribution_delta_rub"] = str(
            _decimal(model_summary["economic_contribution_rub"])
            - _decimal(actual_summary["economic_contribution_rub"])
        )
        scenario_acceptance = _acceptance_result(actual_summary, model_summary)
        model_summary.update(
            {
                "acceptance_gross_profit_not_lower": int(
                    scenario_acceptance["gross_profit_not_lower"]
                ),
                "acceptance_fill_rate_not_lower": int(scenario_acceptance["fill_rate_not_lower"]),
                "acceptance_capital_lower_or_gmroi_higher": int(
                    scenario_acceptance["capital_lower_or_gmroi_higher"]
                ),
                "acceptance_passed": int(scenario_acceptance["passed"]),
            }
        )
        model_summary.update(result.diagnostics.as_summary_fields())
        summary_rows.extend([actual_summary, model_summary])
        if keep_detail:
            detail_rows.extend(result.decision_rows)
            daily_rows.extend(result.daily_rows)
            stage_rows.extend(_stage_summary_rows(result, period_days))
        if scenario.scenario_id == selection.base_scenario_id:
            base_result = result

    if base_result is None:
        raise SystemExit(f"base scenario is missing: {selection.base_scenario_id}")
    base_actual = next(
        row
        for row in summary_rows
        if row["scenario_id"] == selection.base_scenario_id and row["strategy"] == "actual"
    )
    base_model = next(
        row
        for row in summary_rows
        if row["scenario_id"] == selection.base_scenario_id and row["strategy"] == "model"
    )
    acceptance = _acceptance_result(base_actual, base_model)
    control_actual = next(
        row
        for row in summary_rows
        if row["scenario_id"] == selection.control_scenario_id and row["strategy"] == "actual"
    )
    control_model = next(
        row
        for row in summary_rows
        if row["scenario_id"] == selection.control_scenario_id and row["strategy"] == "model"
    )
    protected_model_rows = [
        row
        for row in summary_rows
        if row["strategy"] == "model" and _decimal(row.get("grow_weekly_reduction_cap")) > ZERO
    ]
    passing_scenario_ids = sorted(
        row["scenario_id"]
        for row in protected_model_rows
        if _clean(row.get("acceptance_passed")) == "1"
    )

    output = args.output_dir
    output.mkdir(parents=True, exist_ok=True)
    _write_csv(output / "frozen-scenario-summary.csv", summary_rows)
    artifact_filenames = ["frozen-scenario-summary.csv"]
    quick_comparison: list[dict[str, Any]] = []
    if selection.run_mode == RUN_MODE_QUICK:
        quick_comparison = _quick_comparison_rows(
            summary_rows,
            scenario_roles=selection.scenario_roles,
        )
        _write_csv(output / "quick-scenario-comparison.csv", quick_comparison)
        artifact_filenames.append("quick-scenario-comparison.csv")
    else:
        _write_csv(output / "frozen-baseline-decisions.csv", detail_rows)
        _write_csv(output / "frozen-baseline-daily.csv", daily_rows)
        _write_csv(output / "frozen-baseline-stage.csv", stage_rows)
        _write_csv(
            output / "frozen-baseline-period.csv",
            _period_summary_rows(
                daily_rows,
                scenario_id=selection.base_scenario_id,
                date_from=date_from,
                date_to=date_to,
            ),
        )
        _write_csv(
            output / "frozen-baseline-sku.csv",
            _sku_comparison_rows(base_result, period_days),
        )
        artifact_filenames.extend(
            [
                "frozen-baseline-decisions.csv",
                "frozen-baseline-daily.csv",
                "frozen-baseline-stage.csv",
                "frozen-baseline-period.csv",
                "frozen-baseline-sku.csv",
            ]
        )
    summary = {
        "schema": OUTPUT_SCHEMA,
        "source_preflight_manifest_sha256": _sha256(args.preflight_dir / "run-manifest.json"),
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "run_mode": selection.run_mode,
        "diagnostic_only": selection.run_mode == RUN_MODE_QUICK,
        "production_authorized": False,
        "artifact_level": (
            "compact_metrics_only" if selection.run_mode == RUN_MODE_QUICK else "full_detail"
        ),
        "scenario_roles": dict(selection.scenario_roles),
        "source_scenario_roles": source_scenario_roles,
        "quick_acceleration_guard": {
            "enabled": bool(args.quick_acceleration_guard),
            "require_forecast_growth": bool(args.quick_acceleration_guard),
            "cap_to_projected_shortage": bool(args.quick_cap_acceleration_to_shortage),
            "single_open_lot": bool(args.quick_single_open_acceleration_lot),
            "hypothesis_min_shortage_qty": str(args.hypothesis_min_shortage_qty),
            "cautious_min_shortage_qty": str(args.cautious_min_shortage_qty),
            "hypothesis_segment_profile": (args.quick_hypothesis_acceleration_segment_profile),
            "cautious_segment_profile": (args.quick_cautious_acceleration_segment_profile),
        },
        "quick_base_pipeline": {
            "enabled": base_pipeline_profiles_enabled,
            "hypothesis_profile": args.quick_hypothesis_base_pipeline_profile,
            "cautious_profile": args.quick_cautious_base_pipeline_profile,
            "applies_to": "ordinary_min_max_inventory_position",
            "high_confidence_fraction": "1",
            "hypothesis_min_margin_to_cost_ratio": str(
                next(
                    scenario.base_pipeline_min_margin_to_cost_ratio
                    for scenario in scenarios
                    if scenario.scenario_id == selection.scenario_roles["hypothesis"]
                )
            ),
            "cautious_min_margin_to_cost_ratio": str(
                next(
                    scenario.base_pipeline_min_margin_to_cost_ratio
                    for scenario in scenarios
                    if scenario.scenario_id == selection.scenario_roles["cautious"]
                )
            ),
            "hypothesis_lot_risk_boundary": next(
                scenario.base_pipeline_lot_risk_boundary
                for scenario in scenarios
                if scenario.scenario_id == selection.scenario_roles["hypothesis"]
            ),
            "cautious_lot_risk_boundary": next(
                scenario.base_pipeline_lot_risk_boundary
                for scenario in scenarios
                if scenario.scenario_id == selection.scenario_roles["cautious"]
            ),
            "hypothesis_lot_risk_fraction": str(
                next(
                    scenario.base_pipeline_lot_risk_fraction
                    for scenario in scenarios
                    if scenario.scenario_id == selection.scenario_roles["hypothesis"]
                )
            ),
            "cautious_lot_risk_fraction": str(
                next(
                    scenario.base_pipeline_lot_risk_fraction
                    for scenario in scenarios
                    if scenario.scenario_id == selection.scenario_roles["cautious"]
                )
            ),
        },
        "base_scenario_id": selection.base_scenario_id,
        "scenario_count": len(scenarios),
        "control_scenario_id": selection.control_scenario_id,
        "method": {
            "source_mode": "frozen_preflight_only",
            "execution_mode": selection.run_mode,
            "review_mode": "weekly_plus_event_and_daily_stockout_guard_manual_reviews_assumed_accepted;one_updatable_manual_review_per_scenario_sku",
            "hidden_demand_evaluation": "weighted_unmatched_kmp4_site_and_confirmed_reserve_backlog_at_expiry_or_cancellation",
            "signal_inventory_effect": "one_common_fifo_queue; weighted KMP4 and site open quantities are added once to min/max; reserve backlog acts through effective reserve",
            "site_source_mode": "frozen_anonymized_csv_only_no_live_site_queries",
            "historical_stage": "frozen_daily_stage_from_preflight",
            "historical_sales": "frozen_sparse_sales_from_history_start_through_test_end_used_for_completed_acceleration_and_forecast_error_windows",
            "inventory_position": "simulated_stock_minus_max(raw_historical_reserve,0)+risk_adjusted_simulated_free_pipeline; negative raw reserve never increases availability",
            "base_pipeline_confidence_haircut": (
                "ordinary_min_max_counts_high_confidence_pipeline_at_100_percent;medium_confidence_pipeline_uses_the_role_fraction_only_when_current_margin_to_cost_ratio_meets_the_profile_floor;lot_risk_profiles_count_medium_pipeline_at_95_percent_except_each_lot_with_frozen_remaining_lead_time_strictly_above_the_current_p50_or_p75_boundary_at_90_percent;unknown_or_zero_current_cost_does_not_pass_the_segment_gate;current_decision_features_only;no_future_outcome_filter"
                if base_pipeline_profiles_enabled
                else "off_full_pipeline_counted"
            ),
            "lead_time_usage": "p50_for_simulated_arrival_and_p75_for_positive_economic_service_or_acceleration_coverage",
            "economic_safety_stock": "completed_underforecast_error_capped_by_margin_vs_holding_cost_and_applied_only_above_the_grow_service_floor",
            "grow_service_floor": "sale_stage_p75_or_p90_minimum;budgeted_p90_has_per_sku_and_concurrent_stage_value_caps_with_marginal_saved_margin_allocation",
            "grow_acceleration": (
                "sale_stage_completed_recent_rate_above_prior_window_multiplier_and_current_forecast;remaining_projected_shortage_to_p75_after_full_open_acceleration_protection_at_or_above_role_threshold;acceleration_add_on_capped_by_remaining_shortage;one_open_acceleration_lot_per_sku;new_acceleration_order_component_only_on_shortage_growth;open_protection_released_on_arrival;ordinary_grow_target_state_excludes_acceleration;no_per_sku_economic_quantity_cap;concurrent_stage_budget_ranked_by_expected_saved_margin_per_purchase_ruble;manual_review;ordinary_pipeline_fraction_1_0_0_75_0_5_and_open_acceleration_pipeline_fraction_1"
                if args.quick_single_open_acceleration_lot
                else (
                    "sale_stage_completed_recent_rate_above_prior_window_multiplier_and_current_forecast;projected_shortage_to_p75_at_or_above_role_threshold;acceleration_add_on_capped_by_projected_shortage_to_p75;no_per_sku_economic_quantity_cap;concurrent_stage_budget_ranked_by_expected_saved_margin_per_purchase_ruble;manual_review;pipeline_fraction_1_0_0_75_0_5_by_lead_time_confidence"
                    if args.quick_cap_acceleration_to_shortage
                    else (
                        "sale_stage_completed_recent_rate_above_prior_window_multiplier_and_current_forecast;projected_shortage_to_p75_at_or_above_role_threshold;protect_max_of_p90_and_acceleration_gap_without_per_sku_economic_quantity_cap;concurrent_stage_budget_ranked_by_expected_saved_margin_per_purchase_ruble;manual_review;pipeline_fraction_1_0_0_75_0_5_by_lead_time_confidence"
                        if args.quick_acceleration_guard
                        else "sale_stage_completed_recent_7_or_14_day_rate_vs_prior_28_or_42_days;protect_max_of_p90_and_acceleration_gap_without_per_sku_economic_quantity_cap;concurrent_stage_budget_ranked_by_expected_saved_margin_per_purchase_ruble;manual_review;pipeline_fraction_1_0_0_75_0_5_by_lead_time_confidence"
                    )
                )
            ),
            "acceleration_segmentation": (
                "static_completed_52_week_demand_pattern_as_of_test_start;"
                "current_unit_cost_strictly_below_profile_cap;"
                "current_lead_time_confidence_in_profile;current_p75_at_or_below_profile_cap;"
                "no_future_sku_outcome_filter"
                if segment_profiles_enabled
                else "off"
            ),
            "grow_weekly_target_protection": "min_max_reduction_limited_to_10_20_30_percent_per_scheduled_week;event_reviews_may_raise_not_lower",
            "grow_entry_protection": "entry_min_max_may_rise_but_not_fall_for_2_4_6_weeks",
            "focused_scenario_design": (
                "three_explicit_roles_control_hypothesis_cautious_on_full_frozen_cohort"
                if selection.run_mode == RUN_MODE_QUICK
                else "legacy_plus_prior_control_plus_18_balanced_economic_cap_combinations_plus_three_service_floors_plus_three_targeted_acceleration_profiles"
            ),
            "within_period_launches": "first_observed_positive_stock_seeded_as_exogenous_launch_supply",
            "new_item_reorder_gate": "launch_profile_starts_after_first_positive_stock_or_initial_pipeline_arrival",
        },
        "acceptance": acceptance,
        "protective_scenario_acceptance": {
            "evaluated_count": len(protected_model_rows),
            "passed_count": len(passing_scenario_ids),
            "passing_scenario_ids": passing_scenario_ids,
            "any_passed": bool(passing_scenario_ids),
        },
        "base_actual": base_actual,
        "base_model": base_model,
        "control_actual": control_actual,
        "control_model": control_model,
        "quick_comparison": quick_comparison,
        "files": {filename: _sha256(output / filename) for filename in artifact_filenames},
    }
    (output / "frozen-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
