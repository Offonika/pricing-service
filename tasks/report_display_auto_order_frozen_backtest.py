"""Run the next display auto-order backtest from a frozen PASS preflight only."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from app.services.assortment_lifecycle import AssortmentStatus
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
    "grow_servicefloor_p90_budget_p90_sku50000_stage8000000_"
    "cap20_hold4_typical_kmp0_5_sitebalanced_base"
)
CONTROL_SCENARIO_ID = "grow_cap20_p90_hold4_typical_kmp0_5_sitebalanced_base"
OUTPUT_SCHEMA = "display_auto_order_frozen_backtest.v3"


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
    legacy: bool = False


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
) -> SimulationResult:
    codes = sorted(
        {
            _clean(row.get("nomenclature_code"))
            for rows in fact_rows_by_date.values()
            for row in rows
            if _clean(row.get("nomenclature_code"))
        }
    )
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
    pipeline_qty: dict[str, Decimal] = defaultdict(Decimal)
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
    decision_detail: list[dict[str, Any]] = []
    daily_detail: list[dict[str, Any]] = []
    grow_target_states: dict[str, GrowProtectionState] = {}
    manual_review_seen: set[str] = set()

    cursor = date_from
    while cursor <= date_to:
        for code, qty in arrivals.get(cursor, {}).items():
            stock[code] += qty
            pipeline_qty[code] = max(ZERO, pipeline_qty[code] - qty)
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
            safety_units = ZERO
            economic_safety_cap = ZERO
            percentile_safety_target = ZERO
            service_floor_requested = ZERO
            service_floor_sku_capped = ZERO
            service_floor_allocated = ZERO
            grow_protection_reason = "none"
            manual = not scheduled_review

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
                        safety_units = service_floor_allocated
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
                            safety_units = service_floor_allocated
                        elif (
                            status == AssortmentStatus.SALE.value
                            and scenario.forecast_error_percentile > ZERO
                        ):
                            safety_units = min(economic_safety_cap, percentile_safety_target)
                        else:
                            safety_units = economic_safety_cap
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
            position = stock[code] - reserve + pipeline_qty[code]
            triggered = position <= min_qty
            raw = _ceil(max(ZERO, target_qty - position)) if triggered else ZERO
            recommended = rounded_order_qty(
                raw,
                min_order_qty=policy.min_order_qty,
                max_order_qty=policy.max_order_qty,
                order_rounding_rules=policy.order_rounding_rules,
            )
            manual_review_action = ""
            if recommended > ZERO and manual:
                if code in manual_review_seen:
                    manual_review_action = "updated"
                else:
                    manual_review_seen.add(code)
                    manual_review_action = "created"
            if recommended > ZERO:
                arrival = cursor + timedelta(days=max(1, arrival_lead_days))
                arrivals[arrival][code] += recommended
                pipeline_qty[code] += recommended
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
            if keep_detail and (
                recommended > ZERO or (fresh_decision and (rate > ZERO or weighted_signals > ZERO))
            ):
                trigger = (
                    "scheduled_review"
                    if scheduled_review
                    else "event_review" if fresh_decision else "stockout_guard"
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
                        "service_floor_percentile": str(scenario.grow_service_floor_percentile),
                        "service_floor_requested_qty": str(service_floor_requested),
                        "service_floor_sku_capped_qty": str(service_floor_sku_capped),
                        "service_floor_allocated_qty": str(service_floor_allocated),
                        "service_floor_unfunded_qty": str(
                            max(ZERO, service_floor_requested - service_floor_allocated)
                        ),
                        "service_floor_sku_cap_rub": str(scenario.grow_service_floor_sku_cap_rub),
                        "service_floor_stage_budget_rub": str(
                            scenario.grow_service_floor_stage_budget_rub
                        ),
                        "service_floor_budget_limited": int(
                            service_floor_allocated < service_floor_requested
                        ),
                        "model_stock_qty": str(stock[code]),
                        "reserve_qty": str(reserve),
                        "model_pipeline_qty": str(pipeline_qty[code]),
                        "inventory_position_qty": str(position),
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

    return SimulationResult(
        scenario=scenario,
        actual=actual,
        model=model,
        actual_by_stage=dict(actual_by_stage),
        model_by_stage=dict(model_by_stage),
        decision_rows=decision_detail,
        daily_rows=daily_detail,
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
                    "lost_qty": str(potential - served),
                    "fill_rate": str(served / potential if potential > ZERO else ONE),
                    "gross_profit_rub": str(gross_profit),
                    "average_inventory_value_rub": str(average_inventory),
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
    scenarios = _load_scenarios(args.preflight_dir / "scenario-decisions.csv")
    if not scenarios:
        raise SystemExit("frozen scenario definitions are empty")

    summary_rows: list[dict[str, Any]] = []
    base_result: SimulationResult | None = None
    detail_rows: list[dict[str, Any]] = []
    daily_rows: list[dict[str, Any]] = []
    stage_rows: list[dict[str, Any]] = []
    shared_demand_sample_cache: dict[tuple[str, date, int], list[Decimal]] = {}
    for scenario in scenarios:
        keep_detail = scenario.scenario_id in {
            "legacy",
            CONTROL_SCENARIO_ID,
            BASE_SCENARIO_ID,
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
        summary_rows.extend([actual_summary, model_summary])
        if keep_detail:
            detail_rows.extend(result.decision_rows)
            daily_rows.extend(result.daily_rows)
            stage_rows.extend(_stage_summary_rows(result, period_days))
        if scenario.scenario_id == BASE_SCENARIO_ID:
            base_result = result

    if base_result is None:
        raise SystemExit(f"base scenario is missing: {BASE_SCENARIO_ID}")
    base_actual = next(
        row
        for row in summary_rows
        if row["scenario_id"] == BASE_SCENARIO_ID and row["strategy"] == "actual"
    )
    base_model = next(
        row
        for row in summary_rows
        if row["scenario_id"] == BASE_SCENARIO_ID and row["strategy"] == "model"
    )
    acceptance = _acceptance_result(base_actual, base_model)
    control_actual = next(
        row
        for row in summary_rows
        if row["scenario_id"] == CONTROL_SCENARIO_ID and row["strategy"] == "actual"
    )
    control_model = next(
        row
        for row in summary_rows
        if row["scenario_id"] == CONTROL_SCENARIO_ID and row["strategy"] == "model"
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
    _write_csv(output / "frozen-baseline-decisions.csv", detail_rows)
    _write_csv(output / "frozen-baseline-daily.csv", daily_rows)
    _write_csv(output / "frozen-baseline-stage.csv", stage_rows)
    _write_csv(
        output / "frozen-baseline-period.csv",
        _period_summary_rows(
            daily_rows,
            scenario_id=BASE_SCENARIO_ID,
            date_from=date_from,
            date_to=date_to,
        ),
    )
    _write_csv(
        output / "frozen-baseline-sku.csv",
        _sku_comparison_rows(base_result, period_days),
    )
    summary = {
        "schema": OUTPUT_SCHEMA,
        "source_preflight_manifest_sha256": _sha256(args.preflight_dir / "run-manifest.json"),
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "base_scenario_id": BASE_SCENARIO_ID,
        "scenario_count": len(scenarios),
        "control_scenario_id": CONTROL_SCENARIO_ID,
        "method": {
            "source_mode": "frozen_preflight_only",
            "review_mode": "weekly_plus_event_and_daily_stockout_guard_manual_reviews_assumed_accepted;one_updatable_manual_review_per_scenario_sku",
            "hidden_demand_evaluation": "weighted_unmatched_kmp4_site_and_confirmed_reserve_backlog_at_expiry_or_cancellation",
            "signal_inventory_effect": "one_common_fifo_queue; weighted KMP4 and site open quantities are added once to min/max; reserve backlog acts through effective reserve",
            "site_source_mode": "frozen_anonymized_csv_only_no_live_site_queries",
            "historical_stage": "frozen_daily_stage_from_preflight",
            "inventory_position": "simulated_stock_minus_max(raw_historical_reserve,0)+simulated_free_pipeline; negative raw reserve never increases availability",
            "lead_time_usage": "p50_for_simulated_arrival_and_p75_only_for_economically_protected_coverage",
            "economic_safety_stock": "sale_stage_empirical_p75_p90_p95_completed_underforecast_error_capped_by_margin_vs_holding_cost",
            "grow_weekly_target_protection": "min_max_reduction_limited_to_10_20_30_percent_per_scheduled_week;event_reviews_may_raise_not_lower",
            "grow_entry_protection": "entry_min_max_may_rise_but_not_fall_for_2_4_6_weeks",
            "focused_scenario_design": "legacy_plus_prior_control_plus_18_balanced_incomplete_factorial_protection_combinations",
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
        "files": {
            filename: _sha256(output / filename)
            for filename in (
                "frozen-scenario-summary.csv",
                "frozen-baseline-decisions.csv",
                "frozen-baseline-daily.csv",
                "frozen-baseline-stage.csv",
                "frozen-baseline-period.csv",
                "frozen-baseline-sku.csv",
            )
        },
    }
    (output / "frozen-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
