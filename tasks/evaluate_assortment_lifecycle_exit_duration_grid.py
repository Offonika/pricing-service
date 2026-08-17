"""Compare exit confirmation d3/d5/d7 on the frozen economic training window.

The task simulates only the new d3 and d5 trajectories.  It reuses the already
validated base and d7 economic rows, never consumes the July holdout, and never
performs production or external writes.
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

DEFAULT_D3_TRAJECTORY_HASH = "8beccc1d06b43e87fadd2bcba9662794ea3fee2650ec33faa000e631b6f91cea"
DEFAULT_D5_TRAJECTORY_HASH = "f12fb7499f1bcc966f8843587b44ccb706a7082c846272135f6f41d3e2ae3ca6"
DEFAULT_D7_ECONOMIC_COMPARISON = Path(
    "reports/assortment_lifecycle/backtest-2026-01-01_2026-07-31/"
    "assortment-lifecycle-v2-exit-hysteresis-d7-2026-08-15-v1/"
    "economic-comparison.json"
)
DEFAULT_OUTPUT_DIR = Path(
    "reports/assortment_lifecycle/backtest-2026-01-01_2026-07-31/"
    "exit-hysteresis-duration-grid-2026-08-15-v1"
)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def combine_grid_results(
    *,
    reused_rows: list[Mapping[str, Any]],
    simulated: Mapping[str, Mapping[str, Mapping[str, Any]]],
) -> list[dict[str, Any]]:
    base_by_level = {
        str(row["comparable_group_level"]): row
        for row in reused_rows
        if row["policy"] == "x1.2_base"
    }
    d7_by_level = {
        str(row["comparable_group_level"]): row
        for row in reused_rows
        if row["policy"] == "x1.2_exit_d7"
    }
    if set(base_by_level) != set(GROUP_LEVELS) or set(d7_by_level) != set(GROUP_LEVELS):
        raise ValueError("exit_duration_grid_reused_rows_incomplete")
    combined: list[dict[str, Any]] = []
    for group_level in GROUP_LEVELS:
        base = base_by_level[group_level]
        base_metrics = {metric: base[metric] for metric in MONEY_AND_QUANTITY_METRICS}
        combined.append(dict(base))
        for policy in ("x1.2_exit_d3", "x1.2_exit_d5"):
            metrics = simulated[policy][group_level]
            combined.append(
                {
                    "policy": policy,
                    "comparable_group_level": group_level,
                    **{metric: str(metrics[metric]) for metric in MONEY_AND_QUANTITY_METRICS},
                    **{
                        f"vs_base_{key}": value
                        for key, value in metric_deltas(metrics, base_metrics).items()
                    },
                }
            )
        combined.append(dict(d7_by_level[group_level]))
    return combined


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight-dir", type=Path, default=DEFAULT_PREFLIGHT_DIR)
    parser.add_argument("--replay-store-path", type=Path, default=DEFAULT_REPLAY_STORE)
    parser.add_argument("--dataset-hash", default=DEFAULT_DATASET_HASH)
    parser.add_argument("--d3-trajectory-hash", default=DEFAULT_D3_TRAJECTORY_HASH)
    parser.add_argument("--d5-trajectory-hash", default=DEFAULT_D5_TRAJECTORY_HASH)
    parser.add_argument(
        "--d7-economic-comparison",
        type=Path,
        default=DEFAULT_D7_ECONOMIC_COMPARISON,
    )
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
    reused = _read_json(args.d7_economic_comparison)
    if reused["holdout_consumed"] is not False or reused["production_authorized"] is not False:
        raise ValueError("exit_duration_grid_reused_economics_not_shadow_training")
    store = AssortmentLifecycleReplayStore(args.replay_store_path)
    trajectories = {
        "x1.2_exit_d3": args.d3_trajectory_hash,
        "x1.2_exit_d5": args.d5_trajectory_hash,
    }
    trajectory_metadata = {
        policy: _trajectory_manifest_row(store, trajectory_hash)
        for policy, trajectory_hash in trajectories.items()
    }
    for policy, row in trajectory_metadata.items():
        if row["dataset_hash"] != args.dataset_hash:
            raise ValueError(f"exit_duration_grid_dataset_mismatch:{policy}")

    inputs = load_frozen_inputs(args.preflight_dir, date_to=policy_v2.periods.training_to)
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
    checkpoint_path = args.output_dir / "economic-grid-checkpoint.json"
    if checkpoint_path.exists():
        checkpoint = _read_json(checkpoint_path)
        if checkpoint["trajectories"] != trajectories:
            raise ValueError("exit_duration_grid_checkpoint_lineage_mismatch")
        results: dict[str, dict[str, Mapping[str, Any]]] = checkpoint["results"]
    else:
        results = {}
    application: dict[str, Any] = {}
    shared_demand_sample_cache: dict[tuple[str, date, int], list[Decimal]] = {}
    for policy, trajectory_hash in trajectories.items():
        signature, applied, missing, spike_keys, spike_rates = apply_stored_trajectory(
            store=store,
            trajectory_hash=trajectory_hash,
            fact_by_key=inputs.fact_by_key,
            date_to=policy_v2.periods.training_to,
        )
        application[policy] = {
            "training_trajectory_signature": signature,
            "applied_row_count": applied,
            "trajectory_rows_outside_economic_fact_population": missing,
            "spike_row_count": len(spike_keys),
        }
        policy_results = results.setdefault(policy, {})
        for index, group_level in enumerate(GROUP_LEVELS, start=1):
            if group_level in policy_results:
                print(
                    f"duration grid: {policy} {index}/{len(GROUP_LEVELS)} "
                    f"{group_level} reused=1",
                    flush=True,
                )
                continue
            parameters = _candidate_parameters(group_level)
            representation = RepresentationMinimumLookup(
                eligibility_masks=masks,
                bit=bit_by_variant[(8, group_level)],
                spike_keys=spike_keys,
            )
            result = _simulate(
                inputs=inputs,
                scenario=_scenario_for_candidate(
                    base_scenario,
                    candidate_id=f"{policy}-{group_level}",
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
            policy_results[group_level] = metrics
            _write_json(
                checkpoint_path,
                {
                    "schema": "assortment_lifecycle_exit_duration_grid_checkpoint.v1",
                    "dataset_hash": args.dataset_hash,
                    "trajectories": trajectories,
                    "results": results,
                },
            )
            print(
                f"duration grid: {policy} {index}/{len(GROUP_LEVELS)} " f"{group_level} {metrics}",
                flush=True,
            )
            del result
            gc.collect()

    combined = combine_grid_results(reused_rows=reused["results"], simulated=results)
    payload = {
        "schema": "assortment_lifecycle_exit_duration_grid_economics.v1",
        "status": "training_complete_no_production_decision",
        "period": {
            "warmup_from": "2026-01-01",
            "training_from": policy_v2.periods.training_from.isoformat(),
            "training_to": policy_v2.periods.training_to.isoformat(),
        },
        "dataset_hash": args.dataset_hash,
        "trajectories": {
            policy: {
                "trajectory_hash": row["trajectory_hash"],
                "content_sha256": row["content_sha256"],
            }
            for policy, row in trajectory_metadata.items()
        },
        "reused_d7_economic_comparison": str(args.d7_economic_comparison),
        "application": application,
        "results": combined,
        "holdout_consumed": False,
        "production_authorized": False,
        "production_action": "none_read_only",
    }
    _write_json(args.output_dir / "economic-duration-grid.json", payload)
    print(json.dumps({"status": payload["status"], "rows": len(combined)}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
