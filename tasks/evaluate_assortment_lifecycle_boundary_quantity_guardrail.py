"""Evaluate targeted first-day boundary-entry representation caps on training.

The lifecycle stage remains the immutable x1.2 entry-e1/exit-d3 trajectory.
Only the representation minimum on temporally validated risky boundary dates
is capped.  Ordinary demand, safety stock, strong entries, July holdout, and
all production/external systems remain untouched.
"""

from __future__ import annotations

import argparse
import gc
import json
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from app.services.assortment_lifecycle_replay_store import AssortmentLifecycleReplayStore
from app.services.assortment_lifecycle_v2_policy import (
    DEFAULT_ASSORTMENT_LIFECYCLE_V2_POLICY_PATH,
    load_assortment_lifecycle_v2_policy,
)
from tasks.analyze_assortment_lifecycle_boundary_entry_risk import DEFAULT_OUTPUT_DIR
from tasks.build_display_auto_order_dry_run import load_auto_order_policy
from tasks.display_auto_order_backtest_preflight import (
    load_scenario_config,
    validate_preflight_directory,
)
from tasks.evaluate_assortment_lifecycle_exit_hysteresis_economics import (
    DEFAULT_CONTROL_SCENARIO_ID,
    DEFAULT_DATASET_HASH,
    DEFAULT_PREFLIGHT_DIR,
    DEFAULT_REPLAY_STORE,
    MONEY_AND_QUANTITY_METRICS,
    _candidate_parameters,
    _trajectory_manifest_row,
    apply_stored_trajectory,
    metric_deltas,
)
from tasks.report_display_auto_order_frozen_backtest import _load_scenarios
from tasks.run_assortment_lifecycle_v2_economic_backtest import (
    GROUP_LEVELS,
    RepresentationMinimumLookup,
    _load_item_group_keys,
    _period_metrics,
    _scenario_for_candidate,
    _simulate,
    build_representation_masks,
    load_frozen_inputs,
)

DEFAULT_BASELINE_TRAJECTORY_HASH = (
    "8beccc1d06b43e87fadd2bcba9662794ea3fee2650ec33faa000e631b6f91cea"
)
DEFAULT_X1_2_DURATION_GRID = Path(
    "reports/assortment_lifecycle/backtest-2026-01-01_2026-07-31/"
    "exit-hysteresis-duration-grid-2026-08-15-v1/economic-duration-grid.json"
)
DEFAULT_RISK_DIAGNOSTIC = DEFAULT_OUTPUT_DIR / "boundary-risk-diagnostic.json"
DEFAULT_ECONOMIC_OUTPUT = DEFAULT_OUTPUT_DIR / "economic-quantity-guardrail.json"
DEFAULT_CHECKPOINT = DEFAULT_OUTPUT_DIR / "economic-quantity-guardrail-checkpoint.json"
BASELINE_POLICY = "x1.2_exit_d3"
SCREENING_LEVEL = "all_displays"
REPRESENTATION_CAPS = (Decimal("0"), Decimal("7"), Decimal("10"))


class GuardedRepresentationMinimumLookup:
    def __init__(
        self,
        *,
        base: RepresentationMinimumLookup,
        guarded_keys: set[tuple[date, str]],
        cap_qty: Decimal,
    ) -> None:
        if cap_qty < 0:
            raise ValueError("boundary_quantity_representation_cap_negative")
        self.base = base
        self.guarded_keys = guarded_keys
        self.cap_qty = cap_qty

    def get(self, key: tuple[date, str], default: Any = None) -> Any:
        value = self.base.get(key, default)
        if key not in self.guarded_keys or value is default:
            return value
        if self.cap_qty == 0:
            return default
        return min(Decimal(str(value)), self.cap_qty)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def _candidate_id(cap_qty: Decimal) -> str:
    return f"boundary-risk-ratio-lt1p30-representation-cap-{cap_qty}"


def _candidate_cap(candidate_id: str) -> Decimal:
    prefix = "boundary-risk-ratio-lt1p30-representation-cap-"
    return (
        Decimal(candidate_id.removeprefix(prefix))
        if candidate_id.startswith(prefix)
        else Decimal("0")
    )


def _baseline_by_level(rows: list[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    baseline = {
        str(row["comparable_group_level"]): row
        for row in rows
        if row.get("policy") == BASELINE_POLICY
    }
    if set(baseline) != set(GROUP_LEVELS):
        raise ValueError("boundary_quantity_reused_baseline_incomplete")
    return baseline


def candidate_guardrails(
    candidate: Mapping[str, Any], baseline: Mapping[str, Any]
) -> dict[str, bool]:
    return {
        "served_sales_not_worse": Decimal(str(candidate["served_sales_qty"]))
        >= Decimal(str(baseline["served_sales_qty"])),
        "gross_profit_not_worse": Decimal(str(candidate["gross_profit_rub"]))
        >= Decimal(str(baseline["gross_profit_rub"])),
        "economic_effect_not_worse": Decimal(str(candidate["economic_effect_rub"]))
        >= Decimal(str(baseline["economic_effect_rub"])),
        "gmroi_not_worse": Decimal(str(candidate["gmroi"])) >= Decimal(str(baseline["gmroi"])),
        "ending_excess_not_worse": Decimal(str(candidate["ending_excess_stock_qty"]))
        <= Decimal(str(baseline["ending_excess_stock_qty"])),
    }


def select_screening_winner(
    candidates: Mapping[str, Mapping[str, Any]],
    *,
    baseline: Mapping[str, Any],
) -> str | None:
    passing = [
        (candidate_id, metrics)
        for candidate_id, metrics in candidates.items()
        if all(candidate_guardrails(metrics, baseline).values())
    ]
    if not passing:
        return None
    return max(
        passing,
        key=lambda item: (
            Decimal(str(item[1]["economic_effect_rub"])),
            Decimal(str(item[1]["gmroi"])),
            -Decimal(str(item[1]["ending_excess_stock_qty"])),
            _candidate_cap(item[0]),
        ),
    )[0]


def representation_minimum_reach_by_level(
    *,
    guarded_keys: set[tuple[date, str]],
    masks: Mapping[tuple[date, str], int],
    bit_by_variant: Mapping[tuple[int, str], int],
    spike_keys: set[tuple[date, str]],
) -> dict[str, dict[str, Any]]:
    total = len(guarded_keys)
    reach: dict[str, dict[str, Any]] = {}
    for level in GROUP_LEVELS:
        lookup = RepresentationMinimumLookup(
            eligibility_masks=masks,
            bit=bit_by_variant[(8, level)],
            spike_keys=spike_keys,
        )
        eligible_count = sum(lookup.get(key) == Decimal("13") for key in guarded_keys)
        reach[level] = {
            "guarded_key_count": total,
            "representation_minimum_13_key_count": eligible_count,
            "representation_minimum_13_key_share": eligible_count / total if total else 0.0,
        }
    return reach


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight-dir", type=Path, default=DEFAULT_PREFLIGHT_DIR)
    parser.add_argument("--replay-store-path", type=Path, default=DEFAULT_REPLAY_STORE)
    parser.add_argument("--dataset-hash", default=DEFAULT_DATASET_HASH)
    parser.add_argument("--baseline-trajectory-hash", default=DEFAULT_BASELINE_TRAJECTORY_HASH)
    parser.add_argument("--x1-2-duration-grid", type=Path, default=DEFAULT_X1_2_DURATION_GRID)
    parser.add_argument("--risk-diagnostic", type=Path, default=DEFAULT_RISK_DIAGNOSTIC)
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
    parser.add_argument("--output", type=Path, default=DEFAULT_ECONOMIC_OUTPUT)
    parser.add_argument("--checkpoint", type=Path, default=DEFAULT_CHECKPOINT)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    policy_v2 = load_assortment_lifecycle_v2_policy(args.policy_json)
    validate_preflight_directory(args.preflight_dir)
    reused = _read_json(args.x1_2_duration_grid)
    baseline_by_level = _baseline_by_level(reused["results"])
    if reused["dataset_hash"] != args.dataset_hash:
        raise ValueError("boundary_quantity_reused_dataset_mismatch")
    if reused["holdout_consumed"] is not False:
        raise ValueError("boundary_quantity_reused_holdout_consumed")
    if reused["production_authorized"] is not False:
        raise ValueError("boundary_quantity_reused_production_authorized")

    diagnostic = _read_json(args.risk_diagnostic)
    if diagnostic["dataset_hash"] != args.dataset_hash:
        raise ValueError("boundary_quantity_diagnostic_dataset_mismatch")
    if diagnostic["validation_gates"]["passed"] is not True:
        raise ValueError("boundary_quantity_temporal_validation_required")
    if diagnostic["selection"]["selected_rule_id"] != "growth_ratio_lt_1p30":
        raise ValueError("boundary_quantity_selected_rule_mismatch")
    if diagnostic["july_holdout_consumed"] is not False:
        raise ValueError("boundary_quantity_diagnostic_holdout_consumed")
    guarded_keys = {
        (date.fromisoformat(row["business_date"]), str(row["nomenclature_code"]))
        for row in diagnostic["selected_training_keys"]
    }

    store = AssortmentLifecycleReplayStore(args.replay_store_path)
    trajectory_row = _trajectory_manifest_row(store, args.baseline_trajectory_hash)
    if trajectory_row["dataset_hash"] != args.dataset_hash:
        raise ValueError("boundary_quantity_trajectory_dataset_mismatch")
    inputs = load_frozen_inputs(args.preflight_dir, date_to=policy_v2.periods.training_to)
    signature, applied, missing, spike_keys, spike_rates = apply_stored_trajectory(
        store=store,
        trajectory_hash=args.baseline_trajectory_hash,
        fact_by_key=inputs.fact_by_key,
        date_to=policy_v2.periods.training_to,
    )
    auto_order_policy = load_auto_order_policy(args.auto_order_policy_json)
    scenario_config = load_scenario_config(args.scenario_config_json)
    scenarios = _load_scenarios(args.preflight_dir / "scenario-decisions.csv")
    base_scenario = replace(
        next(row for row in scenarios if row.scenario_id == args.control_scenario_id),
        legacy=False,
    )
    group_keys = _load_item_group_keys(args.replay_store_path, dataset_hash=args.dataset_hash)
    masks, bit_by_variant = build_representation_masks(
        inputs=inputs,
        group_keys_by_code=group_keys,
        group_sizes=(8,),
        group_levels=GROUP_LEVELS,
    )
    representation_minimum_reach = representation_minimum_reach_by_level(
        guarded_keys=guarded_keys,
        masks=masks,
        bit_by_variant=bit_by_variant,
        spike_keys=spike_keys,
    )
    if args.checkpoint.exists():
        checkpoint = _read_json(args.checkpoint)
        if checkpoint.get("trajectory_hash") != args.baseline_trajectory_hash:
            raise ValueError("boundary_quantity_checkpoint_trajectory_mismatch")
        if checkpoint.get("guarded_keys") != len(guarded_keys):
            raise ValueError("boundary_quantity_checkpoint_key_count_mismatch")
        results: dict[str, dict[str, Mapping[str, Any]]] = checkpoint.get("results", {})
    else:
        results = {"screening": {}, "full": {}}

    shared_demand_sample_cache: dict[tuple[str, date, int], list[Decimal]] = {}

    def simulate(cap_qty: Decimal, level: str) -> Mapping[str, Any]:
        candidate_id = _candidate_id(cap_qty)
        representation = GuardedRepresentationMinimumLookup(
            base=RepresentationMinimumLookup(
                eligibility_masks=masks,
                bit=bit_by_variant[(8, level)],
                spike_keys=spike_keys,
            ),
            guarded_keys=guarded_keys,
            cap_qty=cap_qty,
        )
        parameters = _candidate_parameters(level, growth_multiplier="1.2")
        result = _simulate(
            inputs=inputs,
            scenario=_scenario_for_candidate(
                base_scenario,
                candidate_id=f"{candidate_id}-{level}",
                parameters=parameters,
            ),
            policy=auto_order_policy,
            config=scenario_config,
            date_from=date(2026, 1, 1),
            date_to=policy_v2.periods.training_to,
            representation_minimums=representation,
            spike_keys=spike_keys,
            spike_rates=spike_rates,
            demand_sample_cache=shared_demand_sample_cache,
        )
        metrics = _period_metrics(
            result,
            period_from=policy_v2.periods.training_from,
            period_to=policy_v2.periods.training_to,
        )
        del result
        gc.collect()
        return metrics

    for cap_qty in REPRESENTATION_CAPS:
        candidate_id = _candidate_id(cap_qty)
        if candidate_id not in results["screening"]:
            results["screening"][candidate_id] = simulate(cap_qty, SCREENING_LEVEL)
            _write_json(
                args.checkpoint,
                {
                    "schema": "boundary_quantity_guardrail_checkpoint.v1",
                    "trajectory_hash": args.baseline_trajectory_hash,
                    "guarded_keys": len(guarded_keys),
                    "results": results,
                },
            )
        print(
            f"boundary quantity screening {candidate_id}: " f"{results['screening'][candidate_id]}",
            flush=True,
        )

    screening_winner = select_screening_winner(
        results["screening"], baseline=baseline_by_level[SCREENING_LEVEL]
    )
    winner_cap = (
        next(cap for cap in REPRESENTATION_CAPS if _candidate_id(cap) == screening_winner)
        if screening_winner is not None
        else None
    )
    if winner_cap is not None:
        results["full"].setdefault(SCREENING_LEVEL, results["screening"][screening_winner])
        for level in GROUP_LEVELS:
            if level == SCREENING_LEVEL or level in results["full"]:
                continue
            results["full"][level] = simulate(winner_cap, level)
            _write_json(
                args.checkpoint,
                {
                    "schema": "boundary_quantity_guardrail_checkpoint.v1",
                    "trajectory_hash": args.baseline_trajectory_hash,
                    "guarded_keys": len(guarded_keys),
                    "results": results,
                },
            )
            print(
                f"boundary quantity full {screening_winner} {level}: " f"{results['full'][level]}",
                flush=True,
            )

    screening_rows = []
    for cap_qty in REPRESENTATION_CAPS:
        candidate_id = _candidate_id(cap_qty)
        metrics = results["screening"][candidate_id]
        baseline = baseline_by_level[SCREENING_LEVEL]
        screening_rows.append(
            {
                "candidate_id": candidate_id,
                "representation_cap_qty": str(cap_qty),
                "comparable_group_level": SCREENING_LEVEL,
                **{metric: str(metrics[metric]) for metric in MONEY_AND_QUANTITY_METRICS},
                **metric_deltas(metrics, baseline),
                "guardrails": candidate_guardrails(metrics, baseline),
            }
        )
    full_rows = []
    if winner_cap is not None:
        for level in GROUP_LEVELS:
            metrics = results["full"][level]
            baseline = baseline_by_level[level]
            full_rows.append(
                {
                    "candidate_id": screening_winner,
                    "representation_cap_qty": str(winner_cap),
                    "comparable_group_level": level,
                    **{metric: str(metrics[metric]) for metric in MONEY_AND_QUANTITY_METRICS},
                    **metric_deltas(metrics, baseline),
                    "guardrails": candidate_guardrails(metrics, baseline),
                }
            )
    full_passed = bool(full_rows) and all(all(row["guardrails"].values()) for row in full_rows)
    payload = {
        "schema": "assortment_lifecycle_boundary_quantity_guardrail_economics.v1",
        "status": (
            "training_candidate_passed_all_group_levels"
            if full_passed
            else (
                "screening_passed_full_group_validation_failed"
                if screening_winner is not None
                else "screening_complete_no_candidate_passed"
            )
        ),
        "period": {
            "warmup_from": "2026-01-01",
            "training_from": policy_v2.periods.training_from.isoformat(),
            "training_to": policy_v2.periods.training_to.isoformat(),
        },
        "dataset_hash": args.dataset_hash,
        "trajectory": {
            "trajectory_hash": trajectory_row["trajectory_hash"],
            "content_sha256": trajectory_row["content_sha256"],
            "row_count": trajectory_row["row_count"],
        },
        "application": {
            "training_trajectory_signature": signature,
            "applied_row_count": applied,
            "trajectory_rows_outside_economic_fact_population": missing,
            "spike_row_count": len(spike_keys),
        },
        "risk_rule": {
            "rule_id": diagnostic["selection"]["selected_rule_id"],
            "validation_metrics": diagnostic["validation_selected_rule_metrics"],
            "guarded_training_key_count": len(guarded_keys),
        },
        "quantity_guardrail": {
            "component": "representation_minimum_only",
            "baseline_minimum_qty": "13",
            "screening_caps_qty": [str(cap) for cap in REPRESENTATION_CAPS],
            "ordinary_demand_preserved": True,
            "safety_stock_preserved": True,
            "lifecycle_stage_preserved": True,
            "strong_x1.5_entries_preserved": True,
            "representation_minimum_13_reach_by_group_level": (representation_minimum_reach),
        },
        "screening": {
            "group_level": SCREENING_LEVEL,
            "rows": screening_rows,
            "winner": screening_winner,
        },
        "full_group_validation": {
            "rows": full_rows,
            "passed": full_passed,
        },
        "holdout_consumed": False,
        "production_authorized": False,
        "production_action": "none_read_only",
    }
    _write_json(args.output, payload)
    print(
        json.dumps(
            {
                "status": payload["status"],
                "screening_winner": screening_winner,
                "full_group_validation_passed": full_passed,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
