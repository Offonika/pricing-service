"""Backtest working-stage safety stock with proper warm-up and trigger sensitivity.

The task is read-only with respect to databases, 1C, production stages and orders.
It combines two checksum-validated frozen preflights, evaluates February-June
2026 only, and never consumes the July holdout.
"""

from __future__ import annotations

import argparse
import csv
import gc
import hashlib
import json
import os
from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from app.services.assortment_lifecycle_v2_policy import (
    DEFAULT_ASSORTMENT_LIFECYCLE_V2_POLICY_PATH,
    load_assortment_lifecycle_v2_policy,
)
from tasks.build_display_auto_order_dry_run import load_auto_order_policy
from tasks.display_auto_order_backtest_preflight import (
    CarryingCostScenario,
    load_scenario_config,
    validate_preflight_directory,
)
from tasks.report_display_auto_order_frozen_backtest import (
    FrozenScenario,
    SimulationResult,
    _clean,
    _date,
    _decimal,
    _load_scenarios,
    simulate_scenario,
)
from tasks.run_assortment_lifecycle_memory_safe_replay import resource_preflight
from tasks.run_assortment_lifecycle_v2_economic_backtest import (
    GROUP_LEVELS,
    RepresentationMinimumLookup,
    _candidate_id,
    _load_item_group_keys,
    _profile,
    _scenario_for_candidate,
    apply_v2_profile,
    build_representation_masks,
    load_frozen_inputs,
)

ZERO = Decimal("0")
ONE = Decimal("1")
YEAR_DAYS = Decimal("365")
DEFAULT_ROOT = Path("reports/assortment_lifecycle/backtest-2026-01-01_2026-07-31")
DEFAULT_PREFLIGHT_DIR = DEFAULT_ROOT / "preflight"
DEFAULT_WARMUP_PREFLIGHT_DIR = (
    DEFAULT_ROOT / "working-safety-warmup-preflight-2025-08-01_2025-12-31"
)
DEFAULT_REPLAY_DIR = DEFAULT_ROOT / "assortment-lifecycle-v2-memory-safe-replay"
DEFAULT_REPLAY_STORE = Path(".local/assortment-lifecycle-backtest-store.sqlite3")
DEFAULT_DATASET_HASH = "582e10da08e6968f5e1aa450cd88df5dfb5af6c2b9ba84ad05799ec6ec17a6d1"
DEFAULT_SOURCE_SUMMARY = (
    DEFAULT_ROOT
    / "fact-vs-legacy-vs-v2-business-report-2026-08-14"
    / "stage-auto-order-summary.json"
)
DEFAULT_OUTPUT_DIR = DEFAULT_ROOT / "working-safety-warmup-trigger-backtest-2026-08-14"
DEFAULT_CONTROL_SCENARIO_ID = "grow_cap20_p90_hold4_typical_kmp0_5_sitebalanced_base"


@dataclass(frozen=True)
class ErrorObservation:
    decision_date: date
    available_at: date
    value: Decimal


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def _append_checkpoint(path: Path, row: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, default=str) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _read_checkpoint(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    result: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                row = json.loads(line)
                result[str(row["variant_id"])] = row
    return result


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


def _flatten_decisions(
    rows_by_date: Mapping[date, Sequence[Mapping[str, Any]]],
) -> dict[str, list[Mapping[str, Any]]]:
    result: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for rows in rows_by_date.values():
        for row in rows:
            code = _clean(row.get("nomenclature_code"))
            if code:
                result[code].append(row)
    for rows in result.values():
        rows.sort(key=lambda row: _date(row.get("decision_date")) or date.min)
    return dict(result)


def _merge_decision_rows(
    current: Mapping[date, Sequence[Mapping[str, Any]]],
    warmup: Mapping[date, Sequence[Mapping[str, Any]]],
    *,
    current_from: date,
) -> dict[date, list[Mapping[str, Any]]]:
    merged: dict[date, list[Mapping[str, Any]]] = {
        business_date: list(rows) for business_date, rows in current.items()
    }
    for business_date, rows in warmup.items():
        if business_date >= current_from:
            raise ValueError("warm-up decision rows overlap the evaluated preflight")
        merged[business_date] = list(rows)
    return dict(sorted(merged.items()))


def _sales_overlap_mismatches(
    current_sales: Mapping[str, Mapping[date, Decimal]],
    warmup_sales: Mapping[str, Mapping[date, Decimal]],
    *,
    date_from: date,
    date_to: date,
    codes: set[str] | None = None,
) -> list[tuple[str, date, Decimal, Decimal]]:
    mismatches: list[tuple[str, date, Decimal, Decimal]] = []
    compared_codes = codes if codes is not None else set(current_sales) | set(warmup_sales)
    for code in sorted(compared_codes):
        current = current_sales.get(code, {})
        warmup = warmup_sales.get(code, {})
        days = {day for day in set(current) | set(warmup) if date_from <= day <= date_to}
        for business_date in sorted(days):
            current_qty = current.get(business_date, ZERO)
            warmup_qty = warmup.get(business_date, ZERO)
            if current_qty != warmup_qty:
                mismatches.append((code, business_date, current_qty, warmup_qty))
                if len(mismatches) >= 20:
                    return mismatches
    return mismatches


class ComparableGroupFallback:
    """Latest completed underforecast error per peer SKU, with no future data."""

    def __init__(
        self,
        *,
        decision_rows_by_date: Mapping[date, Sequence[Mapping[str, Any]]],
        sales_by_code: Mapping[str, Mapping[date, Decimal]],
        group_keys_by_code: Mapping[str, tuple[str, str, str, str]],
        group_level: str,
        order_cadence_days: int,
        lookback_days: int,
        minimum_group_size: int,
    ) -> None:
        if group_level not in GROUP_LEVELS:
            raise ValueError(f"unsupported comparable group level: {group_level}")
        self.lookback_days = max(1, int(lookback_days))
        self.minimum_group_size = max(1, int(minimum_group_size))
        self.level_index = GROUP_LEVELS.index(group_level)
        sales_index: dict[str, tuple[tuple[date, ...], tuple[Decimal, ...]]] = {}
        for code, sales in sales_by_code.items():
            days = tuple(sorted(sales))
            running = ZERO
            prefix = [ZERO]
            for business_date in days:
                running += max(ZERO, _decimal(sales[business_date]))
                prefix.append(running)
            sales_index[code] = (days, tuple(prefix))

        def observed_demand(code: str, date_from: date, date_to: date) -> Decimal:
            days, prefix = sales_index.get(code, ((), (ZERO,)))
            left = bisect_right(days, date_from)
            right = bisect_right(days, date_to)
            return prefix[right] - prefix[left]

        observations: dict[str, dict[str, list[ErrorObservation]]] = defaultdict(
            lambda: defaultdict(list)
        )
        for rows in decision_rows_by_date.values():
            for row in rows:
                if _clean(row.get("scheduled_review")) != "1":
                    continue
                code = _clean(row.get("nomenclature_code"))
                decision_date = _date(row.get("decision_date"))
                keys = group_keys_by_code.get(code)
                if not code or decision_date is None or keys is None:
                    continue
                horizon_days = max(1, int(row.get("lead_time_p50_days") or 52)) + max(
                    1, order_cadence_days
                )
                observed_through = decision_date + timedelta(days=horizon_days)
                actual_demand = observed_demand(code, decision_date, observed_through)
                predicted_demand = max(
                    ZERO,
                    _decimal(row.get("forecast_rate_sales")) * Decimal(horizon_days),
                )
                group_key = keys[self.level_index]
                observations[group_key][code].append(
                    ErrorObservation(
                        decision_date=decision_date,
                        available_at=observed_through + timedelta(days=1),
                        value=max(ZERO, actual_demand - predicted_demand),
                    )
                )
        self.observations = {
            group_key: {
                code: tuple(sorted(rows, key=lambda row: (row.available_at, row.decision_date)))
                for code, rows in by_code.items()
            }
            for group_key, by_code in observations.items()
        }
        self.available_dates = {
            (group_key, code): tuple(row.available_at for row in rows)
            for group_key, by_code in self.observations.items()
            for code, rows in by_code.items()
        }
        own_observations: dict[str, tuple[ErrorObservation, ...]] = {}
        for by_code in self.observations.values():
            for code, rows in by_code.items():
                own_observations[code] = rows
        self.own_observations = own_observations
        self.own_available_dates = {
            code: tuple(row.available_at for row in rows) for code, rows in own_observations.items()
        }
        self.cache: dict[tuple[date, str], tuple[Decimal, ...]] = {}
        self.own_cache: dict[tuple[date, str], tuple[Decimal, ...]] = {}

    def own_samples(self, *, as_of: date, code: str) -> tuple[Decimal, ...]:
        cache_key = (as_of, code)
        cached = self.own_cache.get(cache_key)
        if cached is not None:
            return cached
        rows = self.own_observations.get(code, ())
        available = self.own_available_dates.get(code, ())
        earliest = as_of - timedelta(days=self.lookback_days)
        right = bisect_right(available, as_of)
        result = tuple(row.value for row in rows[:right] if row.decision_date >= earliest)
        self.own_cache[cache_key] = result
        return result

    def samples(self, *, as_of: date, group_key: str) -> tuple[Decimal, ...]:
        cache_key = (as_of, group_key)
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        earliest = as_of - timedelta(days=self.lookback_days)
        values: list[Decimal] = []
        for code, rows in self.observations.get(group_key, {}).items():
            available = self.available_dates[(group_key, code)]
            index = bisect_right(available, as_of) - 1
            while index >= 0:
                if rows[index].decision_date >= earliest:
                    values.append(rows[index].value)
                    break
                index -= 1
        result = tuple(values) if len(values) >= self.minimum_group_size else ()
        self.cache[cache_key] = result
        return result


def build_fallback_schedule(
    *,
    fact_rows_by_date: Mapping[date, Sequence[Mapping[str, Any]]],
    decision_rows_by_date: Mapping[date, Sequence[Mapping[str, Any]]],
    sales_by_code: Mapping[str, Mapping[date, Decimal]],
    group_keys_by_code: Mapping[str, tuple[str, str, str, str]],
    group_level: str,
    minimum_group_size: int,
    order_cadence_days: int,
    lookback_days: int,
    minimum_samples: int,
    date_from: date,
    date_to: date,
    enable_group_fallback: bool = True,
) -> tuple[
    dict[tuple[str, date, int], tuple[Decimal, ...]],
    dict[tuple[str, date, int], list[Decimal]],
    dict[str, int],
]:
    provider = ComparableGroupFallback(
        decision_rows_by_date=decision_rows_by_date,
        sales_by_code=sales_by_code,
        group_keys_by_code=group_keys_by_code,
        group_level=group_level,
        order_cadence_days=order_cadence_days,
        lookback_days=lookback_days,
        minimum_group_size=minimum_group_size,
    )
    latest: dict[str, Mapping[str, Any]] = {}
    for business_date, rows in decision_rows_by_date.items():
        if business_date >= date_from:
            continue
        for row in rows:
            code = _clean(row.get("nomenclature_code"))
            if code:
                latest[code] = row
    schedule: dict[tuple[str, date, int], tuple[Decimal, ...]] = {}
    own_sample_cache: dict[tuple[str, date, int], list[Decimal]] = {}
    diagnostics = {
        "active_evaluations": 0,
        "own_history_short_evaluations": 0,
        "group_fallback_available_evaluations": 0,
        "group_fallback_missing_evaluations": 0,
    }
    cursor = date_from
    while cursor <= date_to:
        for row in decision_rows_by_date.get(cursor, ()):
            code = _clean(row.get("nomenclature_code"))
            if code:
                latest[code] = row
        for fact in fact_rows_by_date.get(cursor, ()):
            status = _clean(fact.get("status"))
            if status not in {"sale", "working"}:
                continue
            code = _clean(fact.get("nomenclature_code"))
            row = latest.get(code)
            keys = group_keys_by_code.get(code)
            if row is None or keys is None:
                continue
            diagnostics["active_evaluations"] += 1
            p50_horizon = max(1, int(row.get("lead_time_p50_days") or 52)) + max(
                1, order_cadence_days
            )
            p75_horizon = max(
                1,
                int(row.get("lead_time_p75_days") or row.get("lead_time_p50_days") or 52),
            ) + max(1, order_cadence_days)
            own_samples = list(provider.own_samples(as_of=cursor, code=code))
            own_sample_cache[(code, cursor, p50_horizon)] = own_samples
            own_sample_cache[(code, cursor, p75_horizon)] = own_samples
            if len(own_samples) >= minimum_samples:
                continue
            diagnostics["own_history_short_evaluations"] += 1
            if not enable_group_fallback:
                diagnostics["group_fallback_missing_evaluations"] += 1
                continue
            group_samples = provider.samples(
                as_of=cursor,
                group_key=keys[GROUP_LEVELS.index(group_level)],
            )
            if len(group_samples) < minimum_samples:
                diagnostics["group_fallback_missing_evaluations"] += 1
                continue
            diagnostics["group_fallback_available_evaluations"] += 1
            schedule[(code, cursor, p50_horizon)] = group_samples
            schedule[(code, cursor, p75_horizon)] = group_samples
        cursor += timedelta(days=1)
    return schedule, own_sample_cache, diagnostics


def _period_metrics(
    result: SimulationResult,
    *,
    period_from: date,
    period_to: date,
    stage: str | None = None,
    final_status_by_code: Mapping[str, str] | None = None,
) -> dict[str, str]:
    source_rows = result.daily_stage_rows if stage else result.daily_rows
    rows = [
        row
        for row in source_rows
        if period_from <= date.fromisoformat(str(row["business_date"])) <= period_to
        and (stage is None or _clean(row.get("status")) == stage)
    ]
    days = Decimal((period_to - period_from).days + 1)
    observed = sum((_decimal(row.get("actual_observed_demand_qty")) for row in rows), ZERO)
    served = sum((_decimal(row.get("model_served_observed_qty")) for row in rows), ZERO)
    gross_profit = sum((_decimal(row.get("model_gross_profit_rub")) for row in rows), ZERO)
    average_inventory = (
        sum((_decimal(row.get("model_inventory_value_rub")) for row in rows), ZERO) / days
        if rows
        else ZERO
    )
    carrying_cost = average_inventory * result.scenario.cost.total_annual_rate * days / YEAR_DAYS
    gmroi = (
        gross_profit * YEAR_DAYS / days / average_inventory if average_inventory > ZERO else ZERO
    )
    end_rows = [row for row in rows if row.get("business_date") == period_to.isoformat()]
    ending_inventory = sum(
        (_decimal(row.get("model_ending_inventory_qty")) for row in end_rows), ZERO
    )
    ending_excess = ZERO
    for code, metric in result.model.items():
        if stage is not None and (final_status_by_code or {}).get(code) != stage:
            continue
        ending_excess += max(
            ZERO,
            metric.ending_inventory_qty - metric.ending_target_stock_qty,
        )
    decisions = [
        row
        for row in result.decision_rows
        if period_from <= date.fromisoformat(str(row["decision_date"])) <= period_to
        and (stage is None or _clean(row.get("status")) == stage)
    ]
    scoped_model = [
        metric
        for code, metric in result.model.items()
        if stage is None or (final_status_by_code or {}).get(code) == stage
    ]
    return {
        "observed_sales_qty": str(observed),
        "served_sales_qty": str(served),
        "lost_sales_qty": str(observed - served),
        "fill_rate": str(served / observed if observed > ZERO else ZERO),
        "gross_profit_rub": str(gross_profit),
        "average_inventory_value_rub": str(average_inventory),
        "carrying_cost_rub": str(carrying_cost),
        "economic_effect_rub": str(gross_profit - carrying_cost),
        "gmroi": str(gmroi),
        "ending_inventory_qty": str(ending_inventory),
        "ending_excess_stock_qty": str(ending_excess),
        "decision_count": str(len(decisions)),
        "positive_safety_decision_count": str(
            sum(_decimal(row.get("economic_safety_stock_qty")) > ZERO for row in decisions)
        ),
        "positive_trigger_buffer_decision_count": str(
            sum(_decimal(row.get("working_safety_trigger_buffer_qty")) > ZERO for row in decisions)
        ),
        "comparable_group_sample_decision_count": str(
            sum(row.get("safety_sample_source") == "comparable_group" for row in decisions)
        ),
        "recommended_order_qty": str(sum((metric.order_qty for metric in scoped_model), ZERO)),
        "order_line_count": str(sum(metric.order_lines for metric in scoped_model)),
        "ordered_safety_stock_qty": str(
            sum((metric.safety_stock_units_ordered for metric in scoped_model), ZERO)
        ),
    }


def _metric_deltas(candidate: Mapping[str, str], baseline: Mapping[str, str]) -> dict[str, str]:
    names = (
        "served_sales_qty",
        "lost_sales_qty",
        "gross_profit_rub",
        "average_inventory_value_rub",
        "economic_effect_rub",
        "gmroi",
        "ending_inventory_qty",
        "ending_excess_stock_qty",
        "recommended_order_qty",
    )
    return {
        f"{name}_delta": str(_decimal(candidate[name]) - _decimal(baseline[name])) for name in names
    }


def _normalize_evaluation_economics(
    metrics: dict[str, Any],
    *,
    annual_rate: Decimal,
    period_days: int,
) -> None:
    average_inventory = _decimal(metrics["average_inventory_value_rub"])
    gross_profit = _decimal(metrics["gross_profit_rub"])
    carrying_cost = average_inventory * annual_rate * Decimal(period_days) / YEAR_DAYS
    metrics["carrying_cost_rub"] = str(carrying_cost)
    metrics["economic_effect_rub"] = str(gross_profit - carrying_cost)


def _isolated_sample_cache(
    cache: Mapping[tuple[str, date, int], Sequence[Decimal]],
) -> dict[tuple[str, date, int], list[Decimal]]:
    return {key: list(values) for key, values in cache.items()}


def _simulate_variant(
    *,
    variant_id: str,
    base_scenario: FrozenScenario,
    cost: CarryingCostScenario,
    decision_rows_by_date: Mapping[date, Sequence[Mapping[str, Any]]],
    inputs: Any,
    policy: Any,
    config: Any,
    date_from: date,
    date_to: date,
    fallback_demand_samples: Mapping[tuple[str, date, int], Sequence[Decimal]] | None,
    trigger_fraction: Decimal,
    demand_sample_cache: dict[tuple[str, date, int], list[Decimal]],
    spike_keys: set[tuple[date, str]],
    spike_rates: Mapping[tuple[date, str], Decimal],
    representation_minimums: Mapping[tuple[date, str], Decimal],
    working_demand_sample_cache: dict[tuple[str, date, int], list[Decimal]] | None = None,
    working_fallback_demand_samples: (
        Mapping[tuple[str, date, int], Sequence[Decimal]] | None
    ) = None,
    working_safety_unit_cap: int | None = None,
    working_safety_hurdle_multiplier: Decimal = ONE,
    working_safety_require_shortage: bool = False,
    working_safety_min_sales_days: int = 0,
    working_safety_sales_lookback_days: int = 90,
    working_safety_single_open_lot: bool = False,
    keep_decision_detail: bool = True,
) -> SimulationResult:
    scenario = replace(base_scenario, scenario_id=variant_id, cost=cost)
    active_codes = {code for _business_date, code in inputs.fact_by_key}
    isolated_demand_sample_cache = _isolated_sample_cache(demand_sample_cache)
    isolated_working_demand_sample_cache = (
        _isolated_sample_cache(working_demand_sample_cache)
        if working_demand_sample_cache is not None
        else None
    )
    return simulate_scenario(
        scenario=scenario,
        fact_rows_by_date=inputs.fact_rows_by_date,
        decision_rows_by_date=decision_rows_by_date,
        initial_pipeline_rows=[
            row
            for row in inputs.initial_pipeline_rows
            if _clean(row.get("nomenclature_code")) in active_codes
        ],
        sales_by_code=inputs.sales_by_code,
        policy=policy,
        config=config,
        date_from=date_from,
        date_to=date_to,
        keep_detail=True,
        keep_decision_detail=keep_decision_detail,
        keep_loss_detail=False,
        demand_sample_cache=isolated_demand_sample_cache,
        fallback_demand_samples=fallback_demand_samples,
        working_demand_sample_cache=isolated_working_demand_sample_cache,
        working_fallback_demand_samples=working_fallback_demand_samples,
        working_safety_trigger_fraction=trigger_fraction,
        working_safety_unit_cap=working_safety_unit_cap,
        working_safety_hurdle_multiplier=working_safety_hurdle_multiplier,
        working_safety_require_shortage=working_safety_require_shortage,
        working_safety_min_sales_days=working_safety_min_sales_days,
        working_safety_sales_lookback_days=working_safety_sales_lookback_days,
        working_safety_single_open_lot=working_safety_single_open_lot,
        acceleration_allowed_statuses=("sale", "working"),
        acceleration_eligible_sku_dates=spike_keys,
        preclassified_acceleration_rate_by_sku_date=spike_rates,
        representation_minimums=representation_minimums,
    )


def _variants_for_experiment(experiment: str) -> list[dict[str, Any]]:
    baseline = [
        {
            "variant_id": "current_january_warmup_base_t0",
            "history": "current",
            "fallback": False,
            "cost": "base",
            "trigger": ZERO,
        },
        {
            "variant_id": "extended_history_only_base_t0",
            "history": "extended",
            "fallback": False,
            "cost": "base",
            "trigger": ZERO,
        },
    ]
    if experiment == "broad":
        variants = list(baseline)
        for cost_name in ("low", "base", "high"):
            for trigger in (ZERO, Decimal("0.5"), ONE):
                token = str(trigger).replace(".", "p")
                variants.append(
                    {
                        "variant_id": f"extended_group_{cost_name}_t{token}",
                        "history": "extended",
                        "fallback": True,
                        "cost": cost_name,
                        "trigger": trigger,
                    }
                )
        return variants
    if experiment != "targeted":
        raise ValueError(f"unsupported experiment: {experiment}")

    variants = list(baseline)
    for unit_cap in (1, 2, 3):
        for hurdle in (Decimal("1.25"), Decimal("1.5"), Decimal("2.0")):
            hurdle_token = str(hurdle).replace(".", "p")
            variants.append(
                {
                    "variant_id": f"targeted_cap{unit_cap}_h{hurdle_token}",
                    "history": "current",
                    "fallback": False,
                    "working_history": "extended",
                    "working_fallback": True,
                    "cost": "base",
                    "trigger": ONE,
                    "unit_cap": unit_cap,
                    "hurdle_multiplier": hurdle,
                    "require_shortage": True,
                    "min_sales_days": 2,
                    "sales_lookback_days": 90,
                    "single_open_lot": True,
                    "operating_mode": "pooled_central_reserve",
                }
            )
    return variants


def _selected_candidate_audit(
    result: SimulationResult,
    *,
    baseline_result: SimulationResult,
    period_from: date,
    period_to: date,
    final_status_by_code: Mapping[str, str],
    name_by_code: Mapping[str, str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    decisions = [
        row
        for row in result.decision_rows
        if period_from <= date.fromisoformat(str(row["decision_date"])) <= period_to
        and _clean(row.get("status")) == "working"
    ]
    by_code: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "decision_count": 0,
            "positive_safety_decision_count": 0,
            "recommended_order_qty": ZERO,
            "max_safety_qty": ZERO,
            "comparable_group_decision_count": 0,
            "last_safety_sample_source": "",
            "last_projected_shortage_qty": ZERO,
            "last_blocker": "",
        }
    )
    blockers: Counter[str] = Counter()
    for row in decisions:
        code = _clean(row.get("nomenclature_code"))
        if not code:
            continue
        summary = by_code[code]
        safety_qty = _decimal(row.get("economic_safety_stock_qty"))
        summary["decision_count"] += 1
        summary["positive_safety_decision_count"] += int(safety_qty > ZERO)
        summary["recommended_order_qty"] += _decimal(row.get("recommended_order_qty"))
        summary["max_safety_qty"] = max(summary["max_safety_qty"], safety_qty)
        summary["comparable_group_decision_count"] += int(
            row.get("safety_sample_source") == "comparable_group"
        )
        summary["last_safety_sample_source"] = _clean(row.get("safety_sample_source"))
        summary["last_projected_shortage_qty"] = _decimal(
            row.get("working_safety_projected_shortage_qty")
        )
        summary["last_blocker"] = _clean(row.get("working_safety_blocker"))
        if summary["last_blocker"]:
            blockers[summary["last_blocker"]] += 1

    sku_rows: list[dict[str, Any]] = []
    ending_excess_delta_total = ZERO
    positive_ending_excess_delta_total = ZERO
    positive_ending_excess_delta_sku_count = 0
    common_reference_baseline_excess = ZERO
    common_reference_selected_excess = ZERO
    positive_excess_driver_counts: Counter[str] = Counter()
    positive_excess_driver_qty: dict[str, Decimal] = defaultdict(Decimal)
    for code, metric in result.model.items():
        if final_status_by_code.get(code) != "working":
            continue
        baseline_metric = baseline_result.model.get(code)
        if baseline_metric is None:
            raise ValueError(f"baseline result is missing SKU {code}")
        audit = by_code.get(code, {})
        baseline_ending_excess = max(
            ZERO,
            baseline_metric.ending_inventory_qty - baseline_metric.ending_target_stock_qty,
        )
        selected_ending_excess = max(
            ZERO,
            metric.ending_inventory_qty - metric.ending_target_stock_qty,
        )
        common_reference_baseline_excess += baseline_ending_excess
        common_reference_selected_excess += max(
            ZERO,
            metric.ending_inventory_qty - baseline_metric.ending_target_stock_qty,
        )
        ending_excess_delta = selected_ending_excess - baseline_ending_excess
        ending_excess_delta_total += ending_excess_delta
        if ending_excess_delta > ZERO:
            positive_ending_excess_delta_total += ending_excess_delta
            positive_ending_excess_delta_sku_count += 1
            inventory_delta = metric.ending_inventory_qty - baseline_metric.ending_inventory_qty
            target_delta = metric.ending_target_stock_qty - baseline_metric.ending_target_stock_qty
            if inventory_delta > ZERO and target_delta < ZERO:
                driver = "inventory_up_and_target_down"
            elif inventory_delta > ZERO:
                driver = "inventory_up"
            elif target_delta < ZERO:
                driver = "target_down_without_inventory_growth"
            else:
                driver = "other"
            positive_excess_driver_counts[driver] += 1
            positive_excess_driver_qty[driver] += ending_excess_delta
        if (
            metric.safety_stock_units_ordered <= ZERO
            and ending_excess_delta == ZERO
            and metric.served_observed_qty == baseline_metric.served_observed_qty
            and metric.order_qty == baseline_metric.order_qty
        ):
            continue
        sku_rows.append(
            {
                "nomenclature_code": code,
                "name": (name_by_code or {}).get(code, ""),
                "final_status": "working",
                "simulation_served_sales_qty_baseline": str(baseline_metric.served_observed_qty),
                "simulation_served_sales_qty_selected": str(metric.served_observed_qty),
                "simulation_served_sales_qty_delta": str(
                    metric.served_observed_qty - baseline_metric.served_observed_qty
                ),
                "simulation_lost_sales_qty_baseline": str(baseline_metric.lost_observed_qty),
                "simulation_lost_sales_qty_selected": str(metric.lost_observed_qty),
                "simulation_lost_sales_qty_delta": str(
                    metric.lost_observed_qty - baseline_metric.lost_observed_qty
                ),
                "simulation_gross_profit_rub_delta": str(
                    metric.gross_profit_rub - baseline_metric.gross_profit_rub
                ),
                "order_qty_baseline": str(baseline_metric.order_qty),
                "order_qty_selected": str(metric.order_qty),
                "order_qty_delta": str(metric.order_qty - baseline_metric.order_qty),
                "order_value_rub_delta": str(
                    metric.order_value_rub - baseline_metric.order_value_rub
                ),
                "ordered_safety_stock_qty": str(metric.safety_stock_units_ordered),
                "ending_inventory_qty_baseline": str(baseline_metric.ending_inventory_qty),
                "ending_inventory_qty_selected": str(metric.ending_inventory_qty),
                "ending_inventory_qty_delta": str(
                    metric.ending_inventory_qty - baseline_metric.ending_inventory_qty
                ),
                "ending_target_stock_qty_baseline": str(baseline_metric.ending_target_stock_qty),
                "ending_target_stock_qty_selected": str(metric.ending_target_stock_qty),
                "ending_excess_stock_qty_baseline": str(baseline_ending_excess),
                "ending_excess_stock_qty_selected": str(selected_ending_excess),
                "ending_excess_stock_qty_delta": str(ending_excess_delta),
                "decision_count": audit.get("decision_count", 0),
                "positive_safety_decision_count": audit.get("positive_safety_decision_count", 0),
                "recommended_order_qty_from_decisions": str(
                    audit.get("recommended_order_qty", ZERO)
                ),
                "max_safety_qty": str(audit.get("max_safety_qty", ZERO)),
                "comparable_group_decision_count": audit.get("comparable_group_decision_count", 0),
                "last_safety_sample_source": audit.get("last_safety_sample_source", ""),
                "last_projected_shortage_qty": str(audit.get("last_projected_shortage_qty", ZERO)),
                "last_blocker": audit.get("last_blocker", ""),
            }
        )
    sku_rows.sort(
        key=lambda row: (
            _decimal(row["ending_excess_stock_qty_delta"]),
            _decimal(row["ordered_safety_stock_qty"]),
            _decimal(row["simulation_served_sales_qty_delta"]),
        ),
        reverse=True,
    )
    diagnostics = {
        "decision_count": len(decisions),
        "positive_safety_decision_count": sum(
            _decimal(row.get("economic_safety_stock_qty")) > ZERO for row in decisions
        ),
        "positive_order_decision_count": sum(
            _decimal(row.get("recommended_order_qty")) > ZERO for row in decisions
        ),
        "comparable_group_decision_count": sum(
            row.get("safety_sample_source") == "comparable_group" for row in decisions
        ),
        "blocker_counts": dict(sorted(blockers.items())),
        "audited_sku_count": len(sku_rows),
        "ending_excess_stock_qty_delta": str(ending_excess_delta_total),
        "positive_ending_excess_stock_qty_delta": str(positive_ending_excess_delta_total),
        "positive_ending_excess_delta_sku_count": (positive_ending_excess_delta_sku_count),
        "positive_excess_driver_breakdown": {
            name: {
                "sku_count": positive_excess_driver_counts[name],
                "ending_excess_delta_qty": str(quantity),
            }
            for name, quantity in sorted(positive_excess_driver_qty.items())
        },
        "common_reference_target": {
            "definition": "both variants evaluated against baseline ending target per SKU",
            "baseline_ending_excess_stock_qty": str(common_reference_baseline_excess),
            "selected_ending_excess_stock_qty": str(common_reference_selected_excess),
            "ending_excess_stock_qty_delta": str(
                common_reference_selected_excess - common_reference_baseline_excess
            ),
        },
        "metric_scope_note": (
            "ending fields are as of training_to; simulation sales, profit and order "
            "fields cover the full simulation warm-up plus training interval"
        ),
    }
    return sku_rows, diagnostics


def _metric_reconciliation(
    recalculated: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> dict[str, Any]:
    metric_names = (
        "observed_sales_qty",
        "served_sales_qty",
        "lost_sales_qty",
        "gross_profit_rub",
        "average_inventory_value_rub",
        "carrying_cost_rub",
        "economic_effect_rub",
        "gmroi",
        "ending_inventory_qty",
        "ending_excess_stock_qty",
        "recommended_order_qty",
        "order_line_count",
        "ordered_safety_stock_qty",
    )
    deltas = {
        name: str(_decimal(recalculated[name]) - _decimal(expected[name])) for name in metric_names
    }
    return {
        "passed": all(abs(_decimal(value)) <= Decimal("0.000001") for value in deltas.values()),
        "deltas": deltas,
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight-dir", type=Path, default=DEFAULT_PREFLIGHT_DIR)
    parser.add_argument("--warmup-preflight-dir", type=Path, default=DEFAULT_WARMUP_PREFLIGHT_DIR)
    parser.add_argument("--replay-dir", type=Path, default=DEFAULT_REPLAY_DIR)
    parser.add_argument("--replay-store-path", type=Path, default=DEFAULT_REPLAY_STORE)
    parser.add_argument("--dataset-hash", default=DEFAULT_DATASET_HASH)
    parser.add_argument("--source-summary-json", type=Path, default=DEFAULT_SOURCE_SUMMARY)
    parser.add_argument(
        "--policy-json", type=Path, default=DEFAULT_ASSORTMENT_LIFECYCLE_V2_POLICY_PATH
    )
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
    parser.add_argument("--control-scenario-id", default=DEFAULT_CONTROL_SCENARIO_ID)
    parser.add_argument(
        "--experiment",
        choices=("broad", "targeted"),
        default="broad",
    )
    parser.add_argument("--memory-budget-mb", type=int, default=5120)
    parser.add_argument("--min-free-disk-gb", type=int, default=5)
    parser.add_argument("--min-swap-free-mb", type=int, default=256)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    preflight = resource_preflight(
        store_path=args.replay_store_path,
        output_dir=args.output_dir,
        memory_budget_mb=args.memory_budget_mb,
        min_free_disk_gb=args.min_free_disk_gb,
        min_swap_free_mb=args.min_swap_free_mb,
    )
    artifact_resource_preflight = preflight
    previous_summary_path = args.output_dir / "analysis-summary.json"
    if previous_summary_path.exists():
        previous_summary = json.loads(previous_summary_path.read_text(encoding="utf-8"))
        if (
            previous_summary.get("experiment") == args.experiment
            and previous_summary.get("lineage", {}).get("dataset_hash") == args.dataset_hash
        ):
            artifact_resource_preflight = previous_summary.get("resource_preflight", preflight)
    validate_preflight_directory(args.preflight_dir)
    validate_preflight_directory(args.warmup_preflight_dir)
    policy_v2 = load_assortment_lifecycle_v2_policy(args.policy_json)
    period_from = policy_v2.periods.training_from
    period_to = policy_v2.periods.training_to
    simulation_from = date(2026, 1, 1)
    policy = load_auto_order_policy(args.auto_order_policy_json)
    config = load_scenario_config(args.scenario_config_json)
    source_summary = json.loads(args.source_summary_json.read_text(encoding="utf-8"))
    candidate_id = str(source_summary["metadata"]["v2"]["candidate_id"])
    candidate_parameters = dict(source_summary["metadata"]["v2"]["candidate_parameters"])
    if _candidate_id(candidate_parameters) != candidate_id:
        raise SystemExit("source candidate parameters do not reproduce candidate_id")

    inputs = load_frozen_inputs(args.preflight_dir, date_to=period_to)
    warmup_inputs = load_frozen_inputs(
        args.warmup_preflight_dir,
        date_to=simulation_from - timedelta(days=1),
    )
    warmup_manifest = json.loads(
        (args.warmup_preflight_dir / "run-manifest.json").read_text(encoding="utf-8")
    )
    active_codes = {code for _business_date, code in inputs.fact_by_key}
    mismatches = _sales_overlap_mismatches(
        inputs.sales_by_code,
        warmup_inputs.sales_by_code,
        date_from=date.fromisoformat(warmup_manifest["date_from"]),
        date_to=date.fromisoformat(warmup_manifest["date_to"]),
        codes=active_codes,
    )
    if mismatches:
        raise SystemExit(f"warm-up/current sales mismatch: {mismatches[:3]}")
    current_decisions = inputs.decision_rows_by_date
    filtered_warmup_decisions = {
        business_date: [row for row in rows if _clean(row.get("nomenclature_code")) in active_codes]
        for business_date, rows in warmup_inputs.decision_rows_by_date.items()
    }
    combined_decisions = _merge_decision_rows(
        current_decisions,
        filtered_warmup_decisions,
        current_from=simulation_from,
    )

    group_keys = _load_item_group_keys(args.replay_store_path, dataset_hash=args.dataset_hash)
    group_size = int(candidate_parameters["comparable_group_min_size"])
    group_level = str(candidate_parameters["comparable_group_level"])
    masks, bit_by_variant = build_representation_masks(
        inputs=inputs,
        group_keys_by_code=group_keys,
        group_sizes=(group_size,),
        group_levels=(group_level,),
    )
    lifecycle_hash, spike_keys, spike_rates = apply_v2_profile(
        lifecycle_csv=args.replay_dir / "v2-lifecycle-history.csv",
        fact_by_key=inputs.fact_by_key,
        profile=_profile(candidate_parameters),
        date_to=period_to,
    )
    bit = bit_by_variant[(group_size, group_level)]
    representation = RepresentationMinimumLookup(
        eligibility_masks=masks,
        bit=bit,
        spike_keys=spike_keys,
    )

    scenarios = _load_scenarios(args.preflight_dir / "scenario-decisions.csv")
    control = next(row for row in scenarios if row.scenario_id == args.control_scenario_id)
    selected_scenario = _scenario_for_candidate(
        replace(control, legacy=False),
        candidate_id=candidate_id,
        parameters=candidate_parameters,
    )
    costs = {row.name: row for row in config.holding_cost_scenarios}
    if set(costs) != {"low", "base", "high"}:
        raise SystemExit("expected low/base/high holding cost scenarios")

    fallback_schedule, extended_cache, fallback_diagnostics = build_fallback_schedule(
        fact_rows_by_date=inputs.fact_rows_by_date,
        decision_rows_by_date=combined_decisions,
        sales_by_code=inputs.sales_by_code,
        group_keys_by_code=group_keys,
        group_level=group_level,
        minimum_group_size=group_size,
        order_cadence_days=policy.order_cadence_days,
        lookback_days=config.safety_lookback_days,
        minimum_samples=config.safety_min_samples,
        date_from=simulation_from,
        date_to=period_to,
    )
    _unused_fallback, current_cache, current_history_diagnostics = build_fallback_schedule(
        fact_rows_by_date=inputs.fact_rows_by_date,
        decision_rows_by_date=current_decisions,
        sales_by_code=inputs.sales_by_code,
        group_keys_by_code=group_keys,
        group_level=group_level,
        minimum_group_size=group_size,
        order_cadence_days=policy.order_cadence_days,
        lookback_days=config.safety_lookback_days,
        minimum_samples=config.safety_min_samples,
        date_from=simulation_from,
        date_to=period_to,
        enable_group_fallback=False,
    )

    variants = _variants_for_experiment(args.experiment)

    checkpoint_path = args.output_dir / "scenario-checkpoint.jsonl"
    completed = _read_checkpoint(checkpoint_path)
    final_status_by_code = {
        code: _clean(row.get("status"))
        for (business_date, code), row in inputs.fact_by_key.items()
        if business_date == period_to
    }
    for index, variant in enumerate(variants, start=1):
        variant_id = str(variant["variant_id"])
        if variant_id in completed:
            continue
        print(
            json.dumps(
                {"stage": "scenario", "index": index, "count": len(variants), **variant},
                ensure_ascii=False,
                default=str,
            ),
            flush=True,
        )
        extended = variant["history"] == "extended"
        working_extended = variant.get("working_history") == "extended"
        result = _simulate_variant(
            variant_id=variant_id,
            base_scenario=selected_scenario,
            cost=costs[str(variant["cost"])],
            decision_rows_by_date=combined_decisions if extended else current_decisions,
            inputs=inputs,
            policy=policy,
            config=config,
            date_from=simulation_from,
            date_to=period_to,
            fallback_demand_samples=(fallback_schedule if variant["fallback"] else None),
            trigger_fraction=_decimal(variant["trigger"]),
            demand_sample_cache=extended_cache if extended else current_cache,
            working_demand_sample_cache=(extended_cache if working_extended else None),
            working_fallback_demand_samples=(
                fallback_schedule if variant.get("working_fallback") else None
            ),
            spike_keys=spike_keys,
            spike_rates=spike_rates,
            representation_minimums=representation,
            working_safety_unit_cap=variant.get("unit_cap"),
            working_safety_hurdle_multiplier=_decimal(variant.get("hurdle_multiplier", ONE)),
            working_safety_require_shortage=bool(variant.get("require_shortage", False)),
            working_safety_min_sales_days=int(variant.get("min_sales_days", 0)),
            working_safety_sales_lookback_days=int(variant.get("sales_lookback_days", 90)),
            working_safety_single_open_lot=bool(variant.get("single_open_lot", False)),
            keep_decision_detail=False if args.experiment == "targeted" else True,
        )
        row = {
            **variant,
            "annual_carrying_rate": str(result.scenario.cost.total_annual_rate),
            "all": _period_metrics(
                result,
                period_from=period_from,
                period_to=period_to,
                final_status_by_code=final_status_by_code,
            ),
            "working": _period_metrics(
                result,
                period_from=period_from,
                period_to=period_to,
                stage="working",
                final_status_by_code=final_status_by_code,
            ),
        }
        _append_checkpoint(checkpoint_path, row)
        completed[variant_id] = row
        del result
        gc.collect()

    ordered = [completed[str(row["variant_id"])] for row in variants]
    evaluation_annual_rate = costs["base"].total_annual_rate
    evaluation_days = (period_to - period_from).days + 1
    for row in ordered:
        row["safety_decision_annual_rate"] = row.pop(
            "annual_carrying_rate",
            str(costs[str(row["cost"])].total_annual_rate),
        )
        row["evaluation_annual_rate"] = str(evaluation_annual_rate)
        for scope in ("all", "working"):
            _normalize_evaluation_economics(
                row[scope],
                annual_rate=evaluation_annual_rate,
                period_days=evaluation_days,
            )
    baseline = ordered[0]
    history_only = ordered[1]
    summary_rows: list[dict[str, Any]] = []
    for row in ordered:
        flat: dict[str, Any] = {
            "variant_id": row["variant_id"],
            "history": row["history"],
            "fallback": row["fallback"],
            "working_history": row.get("working_history", ""),
            "working_fallback": row.get("working_fallback", ""),
            "cost": row["cost"],
            "trigger": row["trigger"],
            "safety_decision_annual_rate": row["safety_decision_annual_rate"],
            "evaluation_annual_rate": row["evaluation_annual_rate"],
            "unit_cap": row.get("unit_cap", ""),
            "hurdle_multiplier": row.get("hurdle_multiplier", ""),
            "require_shortage": row.get("require_shortage", ""),
            "min_sales_days": row.get("min_sales_days", ""),
            "sales_lookback_days": row.get("sales_lookback_days", ""),
            "single_open_lot": row.get("single_open_lot", ""),
            "operating_mode": row.get("operating_mode", ""),
        }
        for scope in ("all", "working"):
            for name in sorted(row[scope]):
                value = row[scope][name]
                flat[f"{scope}_{name}"] = value
            for name, value in _metric_deltas(row[scope], baseline[scope]).items():
                flat[f"{scope}_{name}_vs_current"] = value
            for name, value in _metric_deltas(row[scope], history_only[scope]).items():
                flat[f"{scope}_{name}_vs_history_only"] = value
        flat["passes_current_guardrails"] = all(
            (
                _decimal(row["all"]["served_sales_qty"])
                >= _decimal(baseline["all"]["served_sales_qty"]),
                _decimal(row["all"]["gross_profit_rub"])
                >= _decimal(baseline["all"]["gross_profit_rub"]),
                _decimal(row["all"]["economic_effect_rub"])
                >= _decimal(baseline["all"]["economic_effect_rub"]),
                _decimal(row["all"]["gmroi"]) >= _decimal(baseline["all"]["gmroi"]),
                _decimal(row["all"]["ending_excess_stock_qty"])
                <= _decimal(baseline["all"]["ending_excess_stock_qty"]),
            )
        )
        summary_rows.append(flat)

    selected_candidate_audit: dict[str, Any] | None = None
    if args.experiment == "targeted":
        candidate_rows = [
            row for row in summary_rows if str(row["variant_id"]).startswith("targeted_")
        ]
        selected_summary = max(
            candidate_rows,
            key=lambda row: _decimal(row["all_economic_effect_rub_delta_vs_current"]),
        )
        selected_variant = next(
            row for row in variants if row["variant_id"] == selected_summary["variant_id"]
        )
        selected_metrics = next(
            row for row in ordered if row["variant_id"] == selected_summary["variant_id"]
        )
        baseline_result = _simulate_variant(
            variant_id=f"{baseline['variant_id']}_audit",
            base_scenario=selected_scenario,
            cost=costs[str(baseline["cost"])],
            decision_rows_by_date=current_decisions,
            inputs=inputs,
            policy=policy,
            config=config,
            date_from=simulation_from,
            date_to=period_to,
            fallback_demand_samples=None,
            trigger_fraction=_decimal(baseline["trigger"]),
            demand_sample_cache=current_cache,
            spike_keys=spike_keys,
            spike_rates=spike_rates,
            representation_minimums=representation,
            keep_decision_detail=False,
        )
        selected_result = _simulate_variant(
            variant_id=f"{selected_variant['variant_id']}_audit",
            base_scenario=selected_scenario,
            cost=costs[str(selected_variant["cost"])],
            decision_rows_by_date=current_decisions,
            inputs=inputs,
            policy=policy,
            config=config,
            date_from=simulation_from,
            date_to=period_to,
            fallback_demand_samples=None,
            trigger_fraction=_decimal(selected_variant["trigger"]),
            demand_sample_cache=current_cache,
            working_demand_sample_cache=extended_cache,
            working_fallback_demand_samples=fallback_schedule,
            spike_keys=spike_keys,
            spike_rates=spike_rates,
            representation_minimums=representation,
            working_safety_unit_cap=int(selected_variant["unit_cap"]),
            working_safety_hurdle_multiplier=_decimal(selected_variant["hurdle_multiplier"]),
            working_safety_require_shortage=True,
            working_safety_min_sales_days=int(selected_variant["min_sales_days"]),
            working_safety_sales_lookback_days=int(selected_variant["sales_lookback_days"]),
            working_safety_single_open_lot=True,
            keep_decision_detail=True,
        )
        audit_reconciliation: dict[str, Any] = {}
        for scope, stage in (("all", None), ("working", "working")):
            baseline_recalculated = _period_metrics(
                baseline_result,
                period_from=period_from,
                period_to=period_to,
                stage=stage,
                final_status_by_code=final_status_by_code,
            )
            selected_recalculated = _period_metrics(
                selected_result,
                period_from=period_from,
                period_to=period_to,
                stage=stage,
                final_status_by_code=final_status_by_code,
            )
            for metrics in (baseline_recalculated, selected_recalculated):
                _normalize_evaluation_economics(
                    metrics,
                    annual_rate=evaluation_annual_rate,
                    period_days=evaluation_days,
                )
            audit_reconciliation[f"baseline_{scope}"] = _metric_reconciliation(
                baseline_recalculated,
                baseline[scope],
            )
            audit_reconciliation[f"selected_{scope}"] = _metric_reconciliation(
                selected_recalculated,
                selected_metrics[scope],
            )
        audit_reconciliation["passed"] = all(
            row["passed"] for name, row in audit_reconciliation.items() if name != "passed"
        )
        if not audit_reconciliation["passed"]:
            raise SystemExit("selected candidate audit does not reproduce scenario metrics")

        name_by_code: dict[str, str] = {}
        for business_date in sorted(current_decisions):
            for row in current_decisions[business_date]:
                code = _clean(row.get("nomenclature_code"))
                name = _clean(row.get("name"))
                if code and name:
                    name_by_code[code] = name
        sku_rows, audit_diagnostics = _selected_candidate_audit(
            selected_result,
            baseline_result=baseline_result,
            period_from=period_from,
            period_to=period_to,
            final_status_by_code=final_status_by_code,
            name_by_code=name_by_code,
        )
        audit_diagnostics["scenario_reconciliation"] = audit_reconciliation
        audit_csv_name = "selected-candidate-sku-audit.csv"
        audit_json_name = "selected-candidate-diagnostics.json"
        _write_csv(args.output_dir / audit_csv_name, sku_rows)
        _write_json(args.output_dir / audit_json_name, audit_diagnostics)
        selected_candidate_audit = {
            "variant_id": selected_variant["variant_id"],
            "selection_rule": "maximum_training_economic_effect_for_diagnostics_only",
            "passes_current_guardrails": selected_summary["passes_current_guardrails"],
            "sku_audit_file": audit_csv_name,
            "diagnostics_file": audit_json_name,
            "diagnostics": audit_diagnostics,
        }
        del baseline_result, selected_result
        gc.collect()

    expected = source_summary["totals"]["v2"]
    baseline_reconciliation = {
        "served_sales_delta_qty": str(
            _decimal(baseline["all"]["served_sales_qty"])
            - _decimal(expected["model_served_recorded_sales_qty"])
        ),
        "average_inventory_delta_rub": str(
            _decimal(baseline["all"]["average_inventory_value_rub"])
            - _decimal(expected["model_average_inventory_value_rub"])
        ),
    }
    reconciliation_passed = _decimal(
        baseline_reconciliation["served_sales_delta_qty"]
    ) == ZERO and abs(_decimal(baseline_reconciliation["average_inventory_delta_rub"])) <= Decimal(
        "0.01"
    )
    payload = {
        "schema": (
            "display_working_targeted_safety_backtest.v2"
            if args.experiment == "targeted"
            else "display_working_safety_warmup_trigger_backtest.v1"
        ),
        "experiment": args.experiment,
        "status": "PASS" if reconciliation_passed else "BLOCKED_BASELINE_MISMATCH",
        "production_authorized": False,
        "production_action": "none_read_only",
        "holdout_consumed": False,
        "period_from": period_from.isoformat(),
        "period_to": period_to.isoformat(),
        "simulation_warmup_from": simulation_from.isoformat(),
        "historical_decisions_from": min(combined_decisions).isoformat(),
        "candidate_id": candidate_id,
        "candidate_parameters": candidate_parameters,
        "lifecycle_hash": lifecycle_hash,
        "fallback_diagnostics": fallback_diagnostics,
        "current_history_diagnostics": current_history_diagnostics,
        "resource_preflight": artifact_resource_preflight,
        "baseline_reconciliation": baseline_reconciliation,
        "scenario_count": len(summary_rows),
        "scenarios": summary_rows,
        "selected_candidate_audit": selected_candidate_audit,
        "targeted_scope_isolation": (
            "extended own-history and comparable-group fallback are applied only to "
            "working; sale keeps the current-history baseline"
            if args.experiment == "targeted"
            else "not_applicable"
        ),
        "operational_alternatives": {
            "network_redistribution": (
                "already represented as perfect pooled network stock; location-level "
                "incremental effect is not identifiable from this frozen aggregate"
            ),
            "central_reserve": (
                "targeted safety units are modeled as pooled central reserve, not as "
                "mandatory store-by-store placement"
            ),
            "more_frequent_replenishment": (
                "not mixed into the targeted quantity grid because the frozen decision "
                "calendar is weekly; requires a separate look-ahead-free decision-calendar run"
            ),
        },
        "lineage": {
            "preflight_manifest_sha256": _sha256(args.preflight_dir / "run-manifest.json"),
            "warmup_preflight_manifest_sha256": _sha256(
                args.warmup_preflight_dir / "run-manifest.json"
            ),
            "source_summary_sha256": _sha256(args.source_summary_json),
            "dataset_hash": args.dataset_hash,
        },
    }
    _write_json(args.output_dir / "analysis-summary.json", payload)
    _write_csv(args.output_dir / "scenario-summary.csv", summary_rows)
    manifest_files = {
        "analysis-summary.json": _sha256(args.output_dir / "analysis-summary.json"),
        "scenario-summary.csv": _sha256(args.output_dir / "scenario-summary.csv"),
        "scenario-checkpoint.jsonl": _sha256(checkpoint_path),
    }
    if selected_candidate_audit is not None:
        for name in (
            selected_candidate_audit["sku_audit_file"],
            selected_candidate_audit["diagnostics_file"],
        ):
            manifest_files[name] = _sha256(args.output_dir / name)
    _write_json(
        args.output_dir / "analysis-manifest.json",
        {
            "schema": (
                "display_working_targeted_safety_manifest.v2"
                if args.experiment == "targeted"
                else "display_working_safety_warmup_trigger_manifest.v1"
            ),
            "complete": True,
            "status": payload["status"],
            "production_authorized": False,
            "production_action": "none_read_only",
            "files": manifest_files,
        },
    )
    print(json.dumps(payload, ensure_ascii=False, default=str), flush=True)
    return 0 if reconciliation_passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
