"""Run the strict lifecycle-v2 economic grid on frozen display facts.

The task is intentionally read-only with respect to databases, 1C and orders.
It walks January as warm-up, evaluates February-June, freezes one candidate,
and only then evaluates that exact candidate on July.  Candidate metrics are
checkpointed as JSONL, so an interrupted long run resumes without repeating
completed simulations.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import itertools
import json
import math
import os
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, MutableMapping, Sequence

from app.services.assortment_lifecycle_facts import _comparable_cost_group_keys
from app.services.assortment_lifecycle_v2_backtest import (
    HOLDOUT_RESULTS_SCHEMA,
    TRAINING_RESULTS_SCHEMA,
    evaluate_selected_holdout,
    select_training_candidate,
)
from app.services.assortment_lifecycle_v2_policy import (
    DEFAULT_ASSORTMENT_LIFECYCLE_V2_POLICY_PATH,
    AssortmentLifecycleV2Policy,
    load_assortment_lifecycle_v2_policy,
)
from tasks.build_display_auto_order_dry_run import load_auto_order_policy
from tasks.display_auto_order_backtest_preflight import (
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
    _read_csv,
    simulate_scenario,
)

ZERO = Decimal("0")
YEAR_DAYS = Decimal("365")
ACTIVE_STATUSES = {"sale", "working"}
GROUP_LEVELS = (
    "brand_quality_construction",
    "quality_construction",
    "quality",
    "all_displays",
)
DEFAULT_DATASET_HASH = "582e10da08e6968f5e1aa450cd88df5dfb5af6c2b9ba84ad05799ec6ec17a6d1"
DEFAULT_CONTROL_SCENARIO_ID = "grow_cap20_p90_hold4_typical_kmp0_5_sitebalanced_base"
DEFAULT_OUTPUT_DIR = Path(
    "reports/assortment_lifecycle/backtest-2026-01-01_2026-07-31/"
    "assortment-lifecycle-v2-economic-backtest"
)
DEFAULT_REPLAY_DIR = Path(
    "reports/assortment_lifecycle/backtest-2026-01-01_2026-07-31/"
    "assortment-lifecycle-v2-memory-safe-replay"
)
DEFAULT_PREFLIGHT_DIR = Path(
    "reports/assortment_lifecycle/backtest-2026-01-01_2026-07-31/preflight"
)
DEFAULT_REPLAY_STORE = Path(".local/assortment-lifecycle-backtest-store.sqlite3")


@dataclass(frozen=True)
class DemandProfile:
    growth_multiplier: Decimal
    confirmation_days: int
    max_single_day_share: Decimal
    min_independent_sales: int

    @property
    def key(self) -> tuple[str, int, str, int]:
        return (
            str(self.growth_multiplier),
            self.confirmation_days,
            str(self.max_single_day_share),
            self.min_independent_sales,
        )


@dataclass
class FrozenInputs:
    fact_rows_by_date: dict[date, list[dict[str, str]]]
    fact_by_key: dict[tuple[date, str], dict[str, str]]
    decision_rows_by_date: dict[date, list[dict[str, str]]]
    sales_by_code: dict[str, dict[date, Decimal]]
    initial_pipeline_rows: list[dict[str, str]]


class RepresentationMinimumLookup:
    def __init__(
        self,
        *,
        eligibility_masks: Mapping[tuple[date, str], int],
        bit: int,
        spike_keys: set[tuple[date, str]],
    ) -> None:
        self.eligibility_masks = eligibility_masks
        self.bit = bit
        self.spike_keys = spike_keys

    def get(self, key: tuple[date, str], default: Any = None) -> Any:
        if key in self.spike_keys:
            return default
        if self.eligibility_masks.get(key, 0) & (1 << self.bit):
            return Decimal("13")
        return default


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _optional_float(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _soft_rate(qty: float, calendar_days: int, available_days: float | None) -> float:
    if qty <= 0:
        return 0.0
    base = qty / calendar_days
    if available_days is None or available_days <= 0:
        return base
    missing = max(0.0, calendar_days - min(available_days, float(calendar_days)))
    return (qty + missing * base) / calendar_days


def _demand_state(
    row: Mapping[str, Any],
    *,
    profile: DemandProfile,
    previous_state: str | None,
    state_since: date | None,
    business_date: date,
) -> tuple[str, date, Decimal]:
    raw_sales = tuple(
        _optional_float(row.get(name)) for name in ("sales_30", "sales_90", "sales_180")
    )
    if any(value is None for value in raw_sales):
        return "no_data", business_date, ZERO
    short, medium, long = (float(value or 0) for value in raw_sales)
    if short == medium == long == 0:
        return "no_sales", business_date, ZERO
    rate_short = _soft_rate(short, 30, _optional_float(row.get("available_days_30")))
    rate_medium = _soft_rate(medium, 90, _optional_float(row.get("available_days_90")))
    rate_long = _soft_rate(long, 180, _optional_float(row.get("available_days_180")))
    declining = rate_short * 1.2 <= rate_medium and rate_medium * 1.2 <= rate_long
    apparent_declining = short / 30 * 1.2 <= medium / 90 and medium / 90 * 1.2 <= long / 180
    confirmed = long >= 12
    if (declining or apparent_declining) and confirmed:
        available_90 = _optional_float(row.get("available_days_90"))
        if available_90 is None or available_90 < 15:
            return "shortage_limited", business_date, Decimal(str(rate_short))
        if not declining:
            return "stable", business_date, Decimal(str(rate_short))
        return "declining", business_date, Decimal(str(rate_short))
    accelerating = rate_short > 0 and rate_short >= rate_medium * float(profile.growth_multiplier)
    if accelerating:
        independent = tuple(
            _optional_float(row.get(name))
            for name in (
                "sales_active_days_30",
                "sales_document_count_30",
                "sales_customer_count_30",
                "sales_point_count_30",
            )
        )
        max_share = _optional_float(row.get("sales_max_day_share_30"))
        complete = all(value is not None for value in (*independent, max_share))
        concentrated = not complete or any(
            float(value or 0) < profile.min_independent_sales for value in independent
        )
        if max_share is not None and max_share > float(profile.max_single_day_share):
            concentrated = True
        held_days = (business_date - state_since).days if state_since is not None else 0
        sustained = (
            previous_state in {"spike", "growing"} and held_days >= profile.confirmation_days
        )
        if confirmed and sustained and not concentrated:
            return "growing", state_since or business_date, Decimal(str(rate_short))
        spike_since = state_since if previous_state == "spike" and state_since else business_date
        return "spike", spike_since, Decimal(str(rate_short))
    if not confirmed:
        return "initial", business_date, Decimal(str(rate_short))
    return "stable", business_date, Decimal(str(rate_short))


def _target_status(
    row: Mapping[str, Any],
    *,
    demand_state: str,
    previous_status: str | None,
) -> str:
    default_status = _clean(row.get("status"))
    if _truthy(row.get("historical_manual_status_replayed")):
        return default_status
    if not _clean(row.get("first_sale_at")):
        return default_status
    blockers = _clean(row.get("blockers"))
    if "sale_without" in blockers or "sale_before" in blockers or demand_state == "no_data":
        return previous_status if previous_status in ACTIVE_STATUSES else "sales_start"
    if demand_state == "growing":
        return "sale"
    if demand_state in {"stable", "declining"}:
        return "working"
    if demand_state == "shortage_limited":
        return previous_status if previous_status in ACTIVE_STATUSES else "sales_start"
    if demand_state in {"no_sales", "initial", "spike"} and previous_status in ACTIVE_STATUSES:
        return str(previous_status)
    return "sales_start"


def apply_v2_profile(
    *,
    lifecycle_csv: Path,
    fact_by_key: MutableMapping[tuple[date, str], dict[str, str]],
    profile: DemandProfile,
    date_to: date,
) -> tuple[str, set[tuple[date, str]], dict[tuple[date, str], Decimal]]:
    previous_status: dict[str, str] = {}
    previous_state: dict[str, str] = {}
    state_since: dict[str, date] = {}
    spike_keys: set[tuple[date, str]] = set()
    spike_rates: dict[tuple[date, str], Decimal] = {}
    digest = hashlib.sha256()
    with lifecycle_csv.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            business_date = date.fromisoformat(row["business_date"])
            if business_date > date_to:
                break
            code = _clean(row.get("nomenclature_code"))
            if not code:
                continue
            prior_status = previous_status.get(code) or _clean(row.get("previous_status"))
            prior_state = previous_state.get(code)
            demand_state, decision_since, rate = _demand_state(
                row,
                profile=profile,
                previous_state=prior_state,
                state_since=state_since.get(code),
                business_date=business_date,
            )
            status = _target_status(row, demand_state=demand_state, previous_status=prior_status)
            previous_status[code] = status
            previous_state[code] = demand_state
            if prior_state == demand_state and demand_state not in {"spike", "growing"}:
                state_since.setdefault(code, decision_since)
            else:
                state_since[code] = decision_since
            key = (business_date, code)
            fact = fact_by_key.get(key)
            if fact is not None:
                fact["previous_status"] = prior_status or ""
                fact["status"] = status
                digest.update(
                    f"{business_date.isoformat()}\0{code}\0{status}\0{demand_state}\0{rate}\n".encode()
                )
                if demand_state == "spike":
                    spike_keys.add(key)
                    if rate > ZERO:
                        spike_rates[key] = rate
    return digest.hexdigest(), spike_keys, spike_rates


def apply_legacy_trajectory(
    *,
    stage_diff_csv: Path,
    fact_by_key: MutableMapping[tuple[date, str], dict[str, str]],
    date_to: date,
) -> str:
    previous: dict[str, str] = {}
    digest = hashlib.sha256()
    with stage_diff_csv.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            business_date = date.fromisoformat(row["business_date"])
            if business_date > date_to:
                break
            code = _clean(row.get("nomenclature_code"))
            status = _clean(row.get("old_status"))
            if not code or not status:
                continue
            key = (business_date, code)
            fact = fact_by_key.get(key)
            if fact is not None:
                fact["previous_status"] = previous.get(code, status)
                fact["status"] = status
                digest.update(f"{business_date.isoformat()}\0{code}\0{status}\n".encode())
            previous[code] = status
    return digest.hexdigest()


def load_frozen_inputs(preflight_dir: Path, *, date_to: date) -> FrozenInputs:
    fact_rows_by_date: dict[date, list[dict[str, str]]] = defaultdict(list)
    fact_by_key: dict[tuple[date, str], dict[str, str]] = {}
    sales_by_code: dict[str, dict[date, Decimal]] = defaultdict(dict)
    for row in _read_csv(preflight_dir / "historical-sales.csv"):
        business_date = _date(row.get("business_date"))
        code = _clean(row.get("nomenclature_code"))
        if business_date is not None and business_date <= date_to and code:
            sales_by_code[code][business_date] = max(ZERO, _decimal(row.get("observed_sales_qty")))
    for row in _read_csv(preflight_dir / "daily-facts.csv"):
        business_date = _date(row.get("business_date"))
        code = _clean(row.get("nomenclature_code"))
        if business_date is None or business_date > date_to or not code:
            continue
        fact_rows_by_date[business_date].append(row)
        fact_by_key[(business_date, code)] = row
        sales_by_code[code][business_date] = _decimal(row.get("observed_sales_qty"))
    decision_rows_by_date: dict[date, list[dict[str, str]]] = defaultdict(list)
    for row in _read_csv(preflight_dir / "decision-inputs.csv"):
        business_date = _date(row.get("decision_date"))
        if business_date is not None and business_date <= date_to:
            decision_rows_by_date[business_date].append(row)
    return FrozenInputs(
        fact_rows_by_date=dict(fact_rows_by_date),
        fact_by_key=fact_by_key,
        decision_rows_by_date=dict(decision_rows_by_date),
        sales_by_code=dict(sales_by_code),
        initial_pipeline_rows=_read_csv(preflight_dir / "initial-pipeline.csv"),
    )


def _load_item_group_keys(
    store_path: Path, *, dataset_hash: str
) -> dict[str, tuple[str, str, str, str]]:
    connection = sqlite3.connect(f"file:{store_path}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT nomenclature_code, payload_json FROM replay_dataset_fact "
            "WHERE dataset_hash = ? AND fact_type = 'item'",
            (dataset_hash,),
        )
        result: dict[str, tuple[str, str, str, str]] = {}
        for code, payload_json in rows:
            keys = _comparable_cost_group_keys(json.loads(payload_json))
            if len(keys) == 4:
                result[str(code)] = tuple(keys)  # type: ignore[assignment]
        return result
    finally:
        connection.close()


def build_representation_masks(
    *,
    inputs: FrozenInputs,
    group_keys_by_code: Mapping[str, tuple[str, str, str, str]],
    group_sizes: Sequence[int],
    group_levels: Sequence[str],
) -> tuple[dict[tuple[date, str], int], dict[tuple[int, str], int]]:
    variants = [(size, level) for size in group_sizes for level in group_levels]
    bit_by_variant = {variant: index for index, variant in enumerate(variants)}
    level_index = {name: GROUP_LEVELS.index(name) for name in group_levels}
    current_cost: dict[str, Decimal] = {}
    masks: dict[tuple[date, str], int] = {}
    for business_date in sorted(inputs.fact_rows_by_date):
        for row in inputs.decision_rows_by_date.get(business_date, ()):
            code = _clean(row.get("nomenclature_code"))
            cost = max(ZERO, _decimal(row.get("inventory_cost_per_unit_rub")))
            if code:
                current_cost[code] = cost
        codes = [
            _clean(row.get("nomenclature_code")) for row in inputs.fact_rows_by_date[business_date]
        ]
        boundaries: dict[tuple[str, str], tuple[int, Decimal]] = {}
        for level in group_levels:
            index = level_index[level]
            grouped: dict[str, list[Decimal]] = defaultdict(list)
            for code in codes:
                keys = group_keys_by_code.get(code)
                cost = current_cost.get(code, ZERO)
                if keys is not None and cost > ZERO:
                    grouped[keys[index]].append(cost)
            for group_key, values in grouped.items():
                values.sort()
                median = values[max(0, math.ceil(len(values) * 0.5) - 1)]
                boundaries[(level, group_key)] = (len(values), median)
        for code in codes:
            keys = group_keys_by_code.get(code)
            cost = current_cost.get(code, ZERO)
            if keys is None or cost <= ZERO:
                continue
            mask = 0
            for size, level in variants:
                sample_size, median = boundaries.get((level, keys[level_index[level]]), (0, ZERO))
                if sample_size >= size and cost <= median:
                    mask |= 1 << bit_by_variant[(size, level)]
            if mask:
                masks[(business_date, code)] = mask
    return masks, bit_by_variant


def _candidate_parameters(policy: AssortmentLifecycleV2Policy) -> list[dict[str, Any]]:
    grid = policy.backtest_grid
    return [
        {
            "growth_multiplier": str(growth),
            "confirmation_days": confirmation,
            "max_single_day_share": str(share),
            "min_independent_sales": independent,
            "spike_quantity_policy": spike,
            "comparable_group_min_size": group_size,
            "comparable_group_level": group_level,
        }
        for growth, confirmation, share, independent, spike, group_size, group_level in itertools.product(
            grid.growth_multipliers,
            grid.confirmation_days,
            grid.max_single_day_shares,
            grid.min_independent_sales,
            grid.spike_quantity_policies,
            grid.comparable_group_min_sizes,
            grid.comparable_group_levels,
        )
    ]


def _candidate_id(parameters: Mapping[str, Any]) -> str:
    return f"display-v2-{_canonical_hash(parameters)[:16]}"


def _profile(parameters: Mapping[str, Any]) -> DemandProfile:
    return DemandProfile(
        growth_multiplier=Decimal(str(parameters["growth_multiplier"])),
        confirmation_days=int(parameters["confirmation_days"]),
        max_single_day_share=Decimal(str(parameters["max_single_day_share"])),
        min_independent_sales=int(parameters["min_independent_sales"]),
    )


def _scenario_for_candidate(
    base: FrozenScenario,
    *,
    candidate_id: str,
    parameters: Mapping[str, Any],
) -> FrozenScenario:
    spike_policy = str(parameters["spike_quantity_policy"])
    if spike_policy == "ordinary_demand_only":
        return replace(
            base,
            scenario_id=candidate_id,
            grow_acceleration_profile="off",
            grow_acceleration_quantity_policy="off",
            grow_acceleration_recent_days=0,
            grow_acceleration_baseline_days=0,
            grow_acceleration_min_recent_sales=ZERO,
            grow_acceleration_rate_multiplier=ZERO,
            grow_acceleration_stage_budget_rub=ZERO,
            grow_acceleration_cap_to_projected_shortage=False,
            grow_acceleration_single_open_lot=False,
        )
    return replace(
        base,
        scenario_id=candidate_id,
        grow_acceleration_profile="lifecycle_spike",
        grow_acceleration_quantity_policy="dynamic_minmax_shortage",
        grow_acceleration_recent_days=30,
        grow_acceleration_baseline_days=60,
        grow_acceleration_min_recent_sales=Decimal("1"),
        grow_acceleration_rate_multiplier=Decimal(str(parameters["growth_multiplier"])),
        grow_acceleration_sku_cap_rub=ZERO,
        grow_acceleration_stage_budget_rub=Decimal("1000000000000000000"),
        grow_acceleration_require_forecast_growth=False,
        grow_acceleration_min_shortage_qty=ZERO,
        grow_acceleration_cap_to_projected_shortage=True,
        grow_acceleration_single_open_lot=(spike_policy == "one_open_lot_projected_shortage_cap"),
    )


def _simulate(
    *,
    inputs: FrozenInputs,
    scenario: FrozenScenario,
    policy: Any,
    config: Any,
    date_from: date,
    date_to: date,
    representation_minimums: Mapping[tuple[date, str], Decimal] | None = None,
    spike_keys: set[tuple[date, str]] | None = None,
    spike_rates: Mapping[tuple[date, str], Decimal] | None = None,
    demand_sample_cache: dict[tuple[str, date, int], list[Decimal]] | None = None,
    keep_decision_detail: bool = False,
    keep_loss_detail: bool = False,
    ordinary_order_overrides: Mapping[tuple[date, str], Decimal] | None = None,
    ordinary_order_topup_qty_overrides: Mapping[tuple[date, str], Decimal] | None = None,
    ordinary_order_topup_arrival_date_overrides: Mapping[tuple[date, str], date] | None = None,
) -> SimulationResult:
    active_codes = {code for _business_date, code in inputs.fact_by_key}
    return simulate_scenario(
        scenario=scenario,
        fact_rows_by_date=inputs.fact_rows_by_date,
        decision_rows_by_date=inputs.decision_rows_by_date,
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
        keep_loss_detail=keep_loss_detail,
        demand_sample_cache=demand_sample_cache,
        acceleration_allowed_statuses=("sale", "working"),
        acceleration_eligible_sku_dates=spike_keys,
        preclassified_acceleration_rate_by_sku_date=spike_rates,
        representation_minimums=representation_minimums,
        ordinary_order_overrides=ordinary_order_overrides,
        ordinary_order_topup_qty_overrides=ordinary_order_topup_qty_overrides,
        ordinary_order_topup_arrival_date_overrides=(ordinary_order_topup_arrival_date_overrides),
    )


def _period_metrics(
    result: SimulationResult, *, period_from: date, period_to: date
) -> dict[str, Decimal]:
    rows = [
        row
        for row in result.daily_rows
        if period_from <= date.fromisoformat(str(row["business_date"])) <= period_to
    ]
    days = Decimal((period_to - period_from).days + 1)
    served = sum((_decimal(row.get("model_served_observed_qty")) for row in rows), ZERO)
    gross_profit = sum((_decimal(row.get("model_gross_profit_rub")) for row in rows), ZERO)
    average_inventory = (
        sum((_decimal(row.get("model_inventory_value_rub")) for row in rows), ZERO) / days
        if rows
        else ZERO
    )
    carrying_cost = average_inventory * result.scenario.cost.total_annual_rate * days / YEAR_DAYS
    economic_effect = gross_profit - carrying_cost
    gmroi = (
        gross_profit * YEAR_DAYS / days / average_inventory if average_inventory > ZERO else ZERO
    )
    ending_excess = sum(
        (
            max(ZERO, metric.ending_inventory_qty - metric.ending_target_stock_qty)
            for metric in result.model.values()
        ),
        ZERO,
    )
    return {
        "served_sales_qty": served,
        "gross_profit_rub": gross_profit,
        "average_inventory_value_rub": average_inventory,
        "carrying_cost_rub": carrying_cost,
        "economic_effect_rub": economic_effect,
        "gmroi": gmroi,
        "ending_inventory_qty": sum(
            (metric.ending_inventory_qty for metric in result.model.values()), ZERO
        ),
        "ending_target_stock_qty": sum(
            (metric.ending_target_stock_qty for metric in result.model.values()), ZERO
        ),
        "ending_excess_stock_qty": ending_excess,
    }


def _deltas(candidate: Mapping[str, Decimal], baseline: Mapping[str, Decimal]) -> dict[str, str]:
    return {
        "served_sales_delta_qty": str(candidate["served_sales_qty"] - baseline["served_sales_qty"]),
        "gross_profit_delta_rub": str(candidate["gross_profit_rub"] - baseline["gross_profit_rub"]),
        "economic_effect_delta_rub": str(
            candidate["economic_effect_rub"] - baseline["economic_effect_rub"]
        ),
        "gmroi_delta": str(candidate["gmroi"] - baseline["gmroi"]),
        "ending_excess_stock_delta_qty": str(
            candidate["ending_excess_stock_qty"] - baseline["ending_excess_stock_qty"]
        ),
    }


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
    rows: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            rows[str(row["candidate_id"])] = row
    return rows


def _rep_signature(
    *,
    fact_by_key: Mapping[tuple[date, str], Mapping[str, Any]],
    masks: Mapping[tuple[date, str], int],
    bit: int,
    spike_keys: set[tuple[date, str]],
) -> str:
    digest = hashlib.sha256()
    for key, mask in masks.items():
        if mask & (1 << bit) and key not in spike_keys:
            fact = fact_by_key.get(key)
            if fact is not None and _clean(fact.get("status")) == "sale":
                digest.update(f"{key[0].isoformat()}\0{key[1]}\n".encode())
    return digest.hexdigest()


def _run_training(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any] | None]:
    policy_v2 = load_assortment_lifecycle_v2_policy(args.policy_json)
    policy_sha = _sha256(args.policy_json)
    validate_preflight_directory(args.preflight_dir)
    inputs = load_frozen_inputs(args.preflight_dir, date_to=policy_v2.periods.training_to)
    auto_order_policy = load_auto_order_policy(args.auto_order_policy_json)
    scenario_config = load_scenario_config(args.scenario_config_json)
    scenarios = _load_scenarios(args.preflight_dir / "scenario-decisions.csv")
    base_scenario = next(row for row in scenarios if row.scenario_id == args.control_scenario_id)
    base_scenario = replace(base_scenario, legacy=False)
    shared_demand_sample_cache: dict[tuple[str, date, int], list[Decimal]] = {}

    print("training: applying legacy trajectory", flush=True)
    legacy_hash = apply_legacy_trajectory(
        stage_diff_csv=args.replay_dir / "stage-diff.csv",
        fact_by_key=inputs.fact_by_key,
        date_to=policy_v2.periods.training_to,
    )
    baseline_result = _simulate(
        inputs=inputs,
        scenario=replace(base_scenario, scenario_id="legacy-stage-control"),
        policy=auto_order_policy,
        config=scenario_config,
        date_from=date(2026, 1, 1),
        date_to=policy_v2.periods.training_to,
        demand_sample_cache=shared_demand_sample_cache,
    )
    baseline_metrics = _period_metrics(
        baseline_result,
        period_from=policy_v2.periods.training_from,
        period_to=policy_v2.periods.training_to,
    )
    _write_json(
        args.output_dir / "training-legacy-baseline.json",
        {"trajectory_hash": legacy_hash, "metrics": baseline_metrics},
    )
    print(f"training: legacy baseline complete {baseline_metrics}", flush=True)

    group_keys = _load_item_group_keys(args.replay_store_path, dataset_hash=args.dataset_hash)
    masks, bit_by_variant = build_representation_masks(
        inputs=inputs,
        group_keys_by_code=group_keys,
        group_sizes=policy_v2.backtest_grid.comparable_group_min_sizes,
        group_levels=policy_v2.backtest_grid.comparable_group_levels,
    )
    parameters = _candidate_parameters(policy_v2)
    checkpoint_path = args.output_dir / "training-candidate-checkpoint.jsonl"
    completed = _read_checkpoint(checkpoint_path)
    simulation_cache: dict[tuple[str, str, str], dict[str, str]] = {}
    profile_groups: dict[tuple[str, int, str, int], list[dict[str, Any]]] = defaultdict(list)
    for row in parameters:
        profile_groups[_profile(row).key].append(row)
    print(
        f"training: {len(parameters)} candidates, {len(profile_groups)} demand profiles, "
        f"{len(completed)} checkpointed",
        flush=True,
    )
    completed_count = len(completed)
    for profile_index, profile_parameters in enumerate(profile_groups.values(), start=1):
        if all(_candidate_id(row) in completed for row in profile_parameters):
            continue
        active_profile = _profile(profile_parameters[0])
        lifecycle_hash, spike_keys, spike_rates = apply_v2_profile(
            lifecycle_csv=args.replay_dir / "v2-lifecycle-history.csv",
            fact_by_key=inputs.fact_by_key,
            profile=active_profile,
            date_to=policy_v2.periods.training_to,
        )
        rep_signatures: dict[tuple[int, str], str] = {}
        for group_size, group_level in {
            (int(row["comparable_group_min_size"]), str(row["comparable_group_level"]))
            for row in profile_parameters
        }:
            bit = bit_by_variant[(group_size, group_level)]
            rep_signatures[(group_size, group_level)] = _rep_signature(
                fact_by_key=inputs.fact_by_key,
                masks=masks,
                bit=bit,
                spike_keys=spike_keys,
            )
        active_spikes = {
            key
            for key in spike_keys
            if _clean(inputs.fact_by_key.get(key, {}).get("status")) in ACTIVE_STATUSES
        }
        print(
            f"training: profile {profile_index}/{len(profile_groups)} "
            f"{active_profile.key}, active_spike_days={len(active_spikes)}",
            flush=True,
        )
        for candidate_parameters in profile_parameters:
            candidate_id = _candidate_id(candidate_parameters)
            if candidate_id in completed:
                continue
            group_variant = (
                int(candidate_parameters["comparable_group_min_size"]),
                str(candidate_parameters["comparable_group_level"]),
            )
            spike_policy = str(candidate_parameters["spike_quantity_policy"])
            policy_signature = spike_policy if active_spikes else "no_active_spike"
            cache_key = (
                lifecycle_hash,
                rep_signatures[group_variant],
                policy_signature,
            )
            metrics = simulation_cache.get(cache_key)
            reused = metrics is not None
            if metrics is None:
                bit = bit_by_variant[group_variant]
                representation = RepresentationMinimumLookup(
                    eligibility_masks=masks,
                    bit=bit,
                    spike_keys=spike_keys,
                )
                scenario = _scenario_for_candidate(
                    base_scenario,
                    candidate_id=candidate_id,
                    parameters=candidate_parameters,
                )
                result = _simulate(
                    inputs=inputs,
                    scenario=scenario,
                    policy=auto_order_policy,
                    config=scenario_config,
                    date_from=date(2026, 1, 1),
                    date_to=policy_v2.periods.training_to,
                    representation_minimums=representation,
                    spike_keys=spike_keys,
                    spike_rates=spike_rates,
                    demand_sample_cache=shared_demand_sample_cache,
                )
                candidate_metrics = _period_metrics(
                    result,
                    period_from=policy_v2.periods.training_from,
                    period_to=policy_v2.periods.training_to,
                )
                metrics = _deltas(candidate_metrics, baseline_metrics)
                simulation_cache[cache_key] = metrics
            checkpoint_row = {
                "candidate_id": candidate_id,
                "parameters": candidate_parameters,
                "metrics": metrics,
                "lifecycle_signature": lifecycle_hash,
                "representation_signature": rep_signatures[group_variant],
                "simulation_reused": reused,
            }
            _append_checkpoint(checkpoint_path, checkpoint_row)
            completed[candidate_id] = checkpoint_row
            completed_count += 1
            print(
                f"training: {completed_count}/{len(parameters)} {candidate_id} "
                f"reused={int(reused)} metrics={metrics}",
                flush=True,
            )

    ordered_candidates = [completed[_candidate_id(row)] for row in parameters]
    payload = {
        "schema": TRAINING_RESULTS_SCHEMA,
        "period_from": policy_v2.periods.training_from.isoformat(),
        "period_to": policy_v2.periods.training_to.isoformat(),
        "dataset_hash": args.dataset_hash,
        "legacy_trajectory_hash": legacy_hash,
        "control_scenario_id": args.control_scenario_id,
        "candidates": [
            {
                "candidate_id": row["candidate_id"],
                "parameters": row["parameters"],
                "metrics": row["metrics"],
            }
            for row in ordered_candidates
        ],
        "production_authorized": False,
        "production_action": "none_read_only",
    }
    _write_json(args.output_dir / "training-results.json", payload)
    selection = select_training_candidate(payload, policy=policy_v2, policy_sha256=policy_sha)
    _write_json(args.output_dir / "selection.json", selection)
    return payload, selection if selection.get("selected_candidate_id") else None


def _run_holdout(args: argparse.Namespace, selection: Mapping[str, Any]) -> dict[str, Any]:
    policy_v2 = load_assortment_lifecycle_v2_policy(args.policy_json)
    policy_sha = _sha256(args.policy_json)
    selected_parameters = dict(selection["selected_parameters"])
    selected_id = str(selection["selected_candidate_id"])
    inputs = load_frozen_inputs(args.preflight_dir, date_to=policy_v2.periods.holdout_to)
    auto_order_policy = load_auto_order_policy(args.auto_order_policy_json)
    scenario_config = load_scenario_config(args.scenario_config_json)
    scenarios = _load_scenarios(args.preflight_dir / "scenario-decisions.csv")
    base_scenario = replace(
        next(row for row in scenarios if row.scenario_id == args.control_scenario_id),
        legacy=False,
    )
    shared_demand_sample_cache: dict[tuple[str, date, int], list[Decimal]] = {}
    print("holdout: selection frozen; July is now consumed once", flush=True)
    legacy_hash = apply_legacy_trajectory(
        stage_diff_csv=args.replay_dir / "stage-diff.csv",
        fact_by_key=inputs.fact_by_key,
        date_to=policy_v2.periods.holdout_to,
    )
    baseline = _simulate(
        inputs=inputs,
        scenario=replace(base_scenario, scenario_id="legacy-stage-control-holdout"),
        policy=auto_order_policy,
        config=scenario_config,
        date_from=date(2026, 1, 1),
        date_to=policy_v2.periods.holdout_to,
        demand_sample_cache=shared_demand_sample_cache,
    )
    baseline_metrics = _period_metrics(
        baseline,
        period_from=policy_v2.periods.holdout_from,
        period_to=policy_v2.periods.holdout_to,
    )
    lifecycle_hash, spike_keys, spike_rates = apply_v2_profile(
        lifecycle_csv=args.replay_dir / "v2-lifecycle-history.csv",
        fact_by_key=inputs.fact_by_key,
        profile=_profile(selected_parameters),
        date_to=policy_v2.periods.holdout_to,
    )
    group_keys = _load_item_group_keys(args.replay_store_path, dataset_hash=args.dataset_hash)
    masks, bit_by_variant = build_representation_masks(
        inputs=inputs,
        group_keys_by_code=group_keys,
        group_sizes=policy_v2.backtest_grid.comparable_group_min_sizes,
        group_levels=policy_v2.backtest_grid.comparable_group_levels,
    )
    variant = (
        int(selected_parameters["comparable_group_min_size"]),
        str(selected_parameters["comparable_group_level"]),
    )
    representation = RepresentationMinimumLookup(
        eligibility_masks=masks,
        bit=bit_by_variant[variant],
        spike_keys=spike_keys,
    )
    candidate = _simulate(
        inputs=inputs,
        scenario=_scenario_for_candidate(
            base_scenario,
            candidate_id=selected_id,
            parameters=selected_parameters,
        ),
        policy=auto_order_policy,
        config=scenario_config,
        date_from=date(2026, 1, 1),
        date_to=policy_v2.periods.holdout_to,
        representation_minimums=representation,
        spike_keys=spike_keys,
        spike_rates=spike_rates,
        demand_sample_cache=shared_demand_sample_cache,
    )
    candidate_metrics = _period_metrics(
        candidate,
        period_from=policy_v2.periods.holdout_from,
        period_to=policy_v2.periods.holdout_to,
    )
    holdout_payload = {
        "schema": HOLDOUT_RESULTS_SCHEMA,
        "period_from": policy_v2.periods.holdout_from.isoformat(),
        "period_to": policy_v2.periods.holdout_to.isoformat(),
        "candidate": {
            "candidate_id": selected_id,
            "parameters": selected_parameters,
            "metrics": _deltas(candidate_metrics, baseline_metrics),
        },
        "lineage": {
            "dataset_hash": args.dataset_hash,
            "legacy_trajectory_hash": legacy_hash,
            "v2_lifecycle_signature": lifecycle_hash,
        },
        "baseline_metrics": baseline_metrics,
        "candidate_metrics": candidate_metrics,
        "production_authorized": False,
        "production_action": "none_read_only",
    }
    _write_json(args.output_dir / "holdout-results.json", holdout_payload)
    decision = evaluate_selected_holdout(
        selection,
        holdout_payload,
        policy=policy_v2,
        policy_sha256=policy_sha,
    )
    _write_json(args.output_dir / "holdout-decision.json", decision)
    return decision


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight-dir", type=Path, default=DEFAULT_PREFLIGHT_DIR)
    parser.add_argument("--replay-dir", type=Path, default=DEFAULT_REPLAY_DIR)
    parser.add_argument("--replay-store-path", type=Path, default=DEFAULT_REPLAY_STORE)
    parser.add_argument("--dataset-hash", default=DEFAULT_DATASET_HASH)
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
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    run_manifest = {
        "schema": "display_assortment_lifecycle_v2_economic_run.v1",
        "status": "running_training",
        "dataset_hash": args.dataset_hash,
        "policy_sha256": _sha256(args.policy_json),
        "preflight_manifest_sha256": _sha256(args.preflight_dir / "run-manifest.json"),
        "holdout_consumed": False,
        "production_authorized": False,
        "production_action": "none_read_only",
    }
    _write_json(args.output_dir / "run-manifest.json", run_manifest)
    training, selection = _run_training(args)
    if selection is None:
        run_manifest.update(
            {
                "status": "complete_no_training_candidate_passed",
                "training_candidate_count": len(training["candidates"]),
                "holdout_consumed": False,
            }
        )
        _write_json(args.output_dir / "run-manifest.json", run_manifest)
        print(json.dumps(run_manifest, ensure_ascii=False, default=str), flush=True)
        return 2
    run_manifest.update(
        {
            "status": "training_selected_holdout_running",
            "selected_candidate_id": selection["selected_candidate_id"],
            "training_candidate_count": len(training["candidates"]),
            "training_pass_count": selection["training_pass_count"],
        }
    )
    _write_json(args.output_dir / "run-manifest.json", run_manifest)
    holdout = _run_holdout(args, selection)
    run_manifest.update(
        {
            "status": "complete",
            "holdout_consumed": True,
            "holdout_decision": holdout["decision"],
            "production_authorized": False,
            "production_action": "none_read_only",
        }
    )
    _write_json(args.output_dir / "run-manifest.json", run_manifest)
    print(json.dumps(run_manifest, ensure_ascii=False, default=str), flush=True)
    return 0 if holdout["decision"] == "eligible_for_diff_review" else 3


if __name__ == "__main__":
    raise SystemExit(main())
