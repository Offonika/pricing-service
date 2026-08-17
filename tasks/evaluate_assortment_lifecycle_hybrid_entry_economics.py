"""Evaluate the x1.2 hybrid-entry + d3 profile on frozen economic training.

The task reuses validated x1.2 entry-e1/exit-d3 baseline rows and simulates
only the new immutable hybrid trajectory.  July economic holdout data is not
consumed and no production or external writes are performed.
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

DEFAULT_CANDIDATE_TRAJECTORY_HASH = (
    "8d2abd30c81e1c4934b5d67a4a231b6664ee13958b9819e575c259d8391f72ab"
)
DEFAULT_X1_2_DURATION_GRID = Path(
    "reports/assortment_lifecycle/backtest-2026-01-01_2026-07-31/"
    "exit-hysteresis-duration-grid-2026-08-15-v1/economic-duration-grid.json"
)
DEFAULT_OUTPUT_DIR = Path(
    "reports/assortment_lifecycle/backtest-2026-01-01_2026-07-31/"
    "hybrid-entry-e1-e2-exit-d3-x1.2-2026-08-15-v1"
)
BASELINE_POLICY = "x1.2_exit_d3"
CANDIDATE_POLICY = "x1.2_hybrid_entry_e1_e2_exit_d3"


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def combine_hybrid_entry_results(
    *,
    reused_rows: list[Mapping[str, Any]],
    simulated: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    baseline_by_level = {
        str(row["comparable_group_level"]): row
        for row in reused_rows
        if row.get("policy") == BASELINE_POLICY
    }
    if set(baseline_by_level) != set(GROUP_LEVELS):
        raise ValueError("hybrid_entry_reused_baseline_incomplete")
    if set(simulated) != set(GROUP_LEVELS):
        raise ValueError("hybrid_entry_simulated_candidate_incomplete")

    combined: list[dict[str, Any]] = []
    for level in GROUP_LEVELS:
        baseline = baseline_by_level[level]
        baseline_metrics = {metric: baseline[metric] for metric in MONEY_AND_QUANTITY_METRICS}
        combined.append(
            {
                "policy": BASELINE_POLICY,
                "comparable_group_level": level,
                **{metric: str(baseline_metrics[metric]) for metric in MONEY_AND_QUANTITY_METRICS},
                **{
                    f"vs_entry_e1_{key}": value
                    for key, value in metric_deltas(baseline_metrics, baseline_metrics).items()
                },
            }
        )
        candidate_metrics = simulated[level]
        combined.append(
            {
                "policy": CANDIDATE_POLICY,
                "comparable_group_level": level,
                **{metric: str(candidate_metrics[metric]) for metric in MONEY_AND_QUANTITY_METRICS},
                **{
                    f"vs_entry_e1_{key}": value
                    for key, value in metric_deltas(candidate_metrics, baseline_metrics).items()
                },
            }
        )
    return combined


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight-dir", type=Path, default=DEFAULT_PREFLIGHT_DIR)
    parser.add_argument("--replay-store-path", type=Path, default=DEFAULT_REPLAY_STORE)
    parser.add_argument("--dataset-hash", default=DEFAULT_DATASET_HASH)
    parser.add_argument("--candidate-trajectory-hash", default=DEFAULT_CANDIDATE_TRAJECTORY_HASH)
    parser.add_argument("--x1-2-duration-grid", type=Path, default=DEFAULT_X1_2_DURATION_GRID)
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
    policy_v2 = load_assortment_lifecycle_v2_policy(args.policy_json)
    validate_preflight_directory(args.preflight_dir)
    reused = _read_json(args.x1_2_duration_grid)
    if reused["dataset_hash"] != args.dataset_hash:
        raise ValueError("hybrid_entry_reused_dataset_mismatch")
    if reused["holdout_consumed"] is not False:
        raise ValueError("hybrid_entry_reused_holdout_consumed")
    if reused["production_authorized"] is not False:
        raise ValueError("hybrid_entry_reused_production_authorized")

    store = AssortmentLifecycleReplayStore(args.replay_store_path)
    trajectory_row = _trajectory_manifest_row(store, args.candidate_trajectory_hash)
    if trajectory_row["dataset_hash"] != args.dataset_hash:
        raise ValueError("hybrid_entry_trajectory_dataset_mismatch")
    if trajectory_row["period_to"] < policy_v2.periods.training_to.isoformat():
        raise ValueError("hybrid_entry_trajectory_period_incomplete")

    inputs = load_frozen_inputs(args.preflight_dir, date_to=policy_v2.periods.training_to)
    signature, applied, missing, spike_keys, spike_rates = apply_stored_trajectory(
        store=store,
        trajectory_hash=args.candidate_trajectory_hash,
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
    checkpoint_path = args.output_dir / "economic-hybrid-entry-checkpoint.json"
    if checkpoint_path.exists():
        checkpoint = _read_json(checkpoint_path)
        if checkpoint.get("trajectory_hash") != args.candidate_trajectory_hash:
            raise ValueError("hybrid_entry_checkpoint_lineage_mismatch")
        results: dict[str, Mapping[str, Any]] = checkpoint.get("results", {})
    else:
        results = {}
    shared_demand_sample_cache: dict[tuple[str, date, int], list[Decimal]] = {}
    for index, level in enumerate(GROUP_LEVELS, start=1):
        if level in results:
            print(
                f"hybrid entry economics: {index}/{len(GROUP_LEVELS)} {level} reused=1",
                flush=True,
            )
            continue
        parameters = _candidate_parameters(level, growth_multiplier="1.2")
        representation = RepresentationMinimumLookup(
            eligibility_masks=masks,
            bit=bit_by_variant[(8, level)],
            spike_keys=spike_keys,
        )
        result = _simulate(
            inputs=inputs,
            scenario=_scenario_for_candidate(
                base_scenario,
                candidate_id=f"{CANDIDATE_POLICY}-{level}",
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
        results[level] = metrics
        _write_json(
            checkpoint_path,
            {
                "schema": "assortment_lifecycle_hybrid_entry_checkpoint.v1",
                "dataset_hash": args.dataset_hash,
                "trajectory_hash": args.candidate_trajectory_hash,
                "results": results,
            },
        )
        print(
            f"hybrid entry economics: {index}/{len(GROUP_LEVELS)} {level} {metrics}",
            flush=True,
        )
        del result
        gc.collect()

    rows = combine_hybrid_entry_results(reused_rows=reused["results"], simulated=results)
    payload = {
        "schema": "assortment_lifecycle_hybrid_entry_economics.v1",
        "status": "training_complete_no_production_decision",
        "period": {
            "warmup_from": "2026-01-01",
            "training_from": policy_v2.periods.training_from.isoformat(),
            "training_to": policy_v2.periods.training_to.isoformat(),
        },
        "dataset_hash": args.dataset_hash,
        "baseline_policy": BASELINE_POLICY,
        "candidate_policy": CANDIDATE_POLICY,
        "candidate_trajectory": {
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
        "methodology": {
            "growth_multiplier": "1.2",
            "strong_entry_threshold": "1.5",
            "strong_entry_confirmation_days": 1,
            "boundary_entry_confirmation_days": 2,
            "exit_confirmation_days": 3,
            "baseline_rows": "reused_validated_x1.2_entry_e1_exit_d3",
            "candidate_rows": "fresh_memory_safe_simulation",
            "economic_holdout": "not_consumed",
            "look_ahead_free": True,
        },
        "results": rows,
        "holdout_consumed": False,
        "production_authorized": False,
        "production_action": "none_read_only",
    }
    _write_json(args.output_dir / "economic-hybrid-entry.json", payload)
    print(json.dumps({"status": payload["status"], "rows": len(rows)}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
