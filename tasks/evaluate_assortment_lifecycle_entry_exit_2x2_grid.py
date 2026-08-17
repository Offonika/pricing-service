"""Evaluate the x1.5 base/d3 cells of the frozen lifecycle 2x2 grid.

The task reuses the validated x1.2 base/d3 economic rows. It freshly simulates
the immutable x1.5 base and d3 trajectories so the four cells use the same
current simulator. The compact historical x1.5 training candidates are retained
only for reconciliation. July holdout and production/external writes are excluded.
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
    DEFAULT_EXISTING_ECONOMIC_DIR,
    DEFAULT_PREFLIGHT_DIR,
    DEFAULT_REPLAY_STORE,
    _candidate_parameters,
    _trajectory_manifest_row,
    apply_stored_trajectory,
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

DEFAULT_X1_5_D3_TRAJECTORY_HASH = "d420db003a82ba24d563ed4a86836a773e9cdffb89f6119e473fbfb41dda914c"
DEFAULT_X1_5_BASE_TRAJECTORY_HASH = (
    "f981f8ec5f72fd42f475b05ab025c66f7979909bedce53c78f64083969a08c4c"
)
DEFAULT_X1_2_DURATION_GRID = Path(
    "reports/assortment_lifecycle/backtest-2026-01-01_2026-07-31/"
    "exit-hysteresis-duration-grid-2026-08-15-v1/economic-duration-grid.json"
)
DEFAULT_OUTPUT_DIR = Path(
    "reports/assortment_lifecycle/backtest-2026-01-01_2026-07-31/"
    "entry-exit-2x2-grid-2026-08-15-v1"
)
DECISION_METRICS = (
    "served_sales_qty",
    "gross_profit_rub",
    "average_inventory_value_rub",
    "economic_effect_rub",
    "gmroi",
    "ending_excess_stock_qty",
)
TOTAL_ANNUAL_CARRY_RATE = Decimal("0.65")
TRAINING_DAYS = Decimal("150")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def _decimal_metrics(row: Mapping[str, Any]) -> dict[str, Decimal]:
    return {metric: Decimal(str(row[metric])) for metric in DECISION_METRICS}


def reconstruct_candidate_metrics(
    *, baseline: Mapping[str, Any], candidate_deltas: Mapping[str, Any]
) -> dict[str, Decimal]:
    """Reconstruct decision metrics from the validated compact training grid."""

    baseline_metrics = {key: Decimal(str(value)) for key, value in baseline.items()}
    gross_profit_delta = Decimal(str(candidate_deltas["gross_profit_delta_rub"]))
    economic_effect_delta = Decimal(str(candidate_deltas["economic_effect_delta_rub"]))
    inventory_delta = (
        (gross_profit_delta - economic_effect_delta)
        * Decimal("365")
        / (TOTAL_ANNUAL_CARRY_RATE * TRAINING_DAYS)
    )
    return {
        "served_sales_qty": baseline_metrics["served_sales_qty"]
        + Decimal(str(candidate_deltas["served_sales_delta_qty"])),
        "gross_profit_rub": baseline_metrics["gross_profit_rub"] + gross_profit_delta,
        "average_inventory_value_rub": baseline_metrics["average_inventory_value_rub"]
        + inventory_delta,
        "economic_effect_rub": baseline_metrics["economic_effect_rub"] + economic_effect_delta,
        "gmroi": baseline_metrics["gmroi"] + Decimal(str(candidate_deltas["gmroi_delta"])),
        "ending_excess_stock_qty": baseline_metrics["ending_excess_stock_qty"]
        + Decimal(str(candidate_deltas["ending_excess_stock_delta_qty"])),
    }


def select_existing_x1_5_candidates(
    training_results: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    selected: dict[str, Mapping[str, Any]] = {}
    for row in training_results.get("candidates", []):
        parameters = row.get("parameters", {})
        level = str(parameters.get("comparable_group_level", ""))
        if level not in GROUP_LEVELS:
            continue
        if all(
            parameters.get(key) == value
            for key, value in _candidate_parameters(level, growth_multiplier="1.5").items()
        ):
            selected[level] = row
    if set(selected) != set(GROUP_LEVELS):
        raise ValueError("entry_exit_2x2_existing_x1_5_candidates_incomplete")
    return selected


def combine_2x2_results(
    *,
    x1_2_rows: list[Mapping[str, Any]],
    x1_5_base: Mapping[str, Mapping[str, Any]],
    x1_5_d3: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    x1_2 = {
        (str(row["policy"]), str(row["comparable_group_level"])): row
        for row in x1_2_rows
        if row.get("policy") in {"x1.2_base", "x1.2_exit_d3"}
    }
    expected = {
        (policy, level) for policy in ("x1.2_base", "x1.2_exit_d3") for level in GROUP_LEVELS
    }
    if set(x1_2) != expected:
        raise ValueError("entry_exit_2x2_reused_x1_2_rows_incomplete")

    combined: list[dict[str, Any]] = []
    for level in GROUP_LEVELS:
        profiles = (
            ("x1.2_base", _decimal_metrics(x1_2[("x1.2_base", level)])),
            ("x1.2_exit_d3", _decimal_metrics(x1_2[("x1.2_exit_d3", level)])),
            ("x1.5_base", _decimal_metrics(x1_5_base[level])),
            ("x1.5_exit_d3", _decimal_metrics(x1_5_d3[level])),
        )
        reference_12 = profiles[0][1]
        reference_15 = profiles[2][1]
        for policy, metrics in profiles:
            row: dict[str, Any] = {
                "policy": policy,
                "comparable_group_level": level,
                **{key: str(value) for key, value in metrics.items()},
            }
            for prefix, reference in (
                ("vs_x1.2_base", reference_12),
                ("vs_x1.5_base", reference_15),
            ):
                row.update(
                    {
                        f"{prefix}_{key}_delta": str(value - reference[key])
                        for key, value in metrics.items()
                    }
                )
            combined.append(row)
    return combined


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight-dir", type=Path, default=DEFAULT_PREFLIGHT_DIR)
    parser.add_argument("--existing-economic-dir", type=Path, default=DEFAULT_EXISTING_ECONOMIC_DIR)
    parser.add_argument("--replay-store-path", type=Path, default=DEFAULT_REPLAY_STORE)
    parser.add_argument("--dataset-hash", default=DEFAULT_DATASET_HASH)
    parser.add_argument("--x1-5-base-trajectory-hash", default=DEFAULT_X1_5_BASE_TRAJECTORY_HASH)
    parser.add_argument("--x1-5-d3-trajectory-hash", default=DEFAULT_X1_5_D3_TRAJECTORY_HASH)
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
    x1_2_grid = _read_json(args.x1_2_duration_grid)
    if x1_2_grid["holdout_consumed"] is not False:
        raise ValueError("entry_exit_2x2_reused_holdout_consumed")
    if x1_2_grid["production_authorized"] is not False:
        raise ValueError("entry_exit_2x2_reused_production_authorized")

    baseline_payload = _read_json(args.existing_economic_dir / "training-legacy-baseline.json")
    training_results = _read_json(args.existing_economic_dir / "training-results.json")
    existing_x1_5 = select_existing_x1_5_candidates(training_results)
    compact_x1_5_base = {
        level: reconstruct_candidate_metrics(
            baseline=baseline_payload["metrics"],
            candidate_deltas=row["metrics"],
        )
        for level, row in existing_x1_5.items()
    }

    store = AssortmentLifecycleReplayStore(args.replay_store_path)
    trajectories = {
        "x1.5_base": args.x1_5_base_trajectory_hash,
        "x1.5_exit_d3": args.x1_5_d3_trajectory_hash,
    }
    trajectory_rows = {
        policy: _trajectory_manifest_row(store, trajectory_hash)
        for policy, trajectory_hash in trajectories.items()
    }
    for policy, trajectory_row in trajectory_rows.items():
        if trajectory_row["dataset_hash"] != args.dataset_hash:
            raise ValueError(f"entry_exit_2x2_trajectory_dataset_mismatch:{policy}")
        if trajectory_row["period_to"] < policy_v2.periods.training_to.isoformat():
            raise ValueError(f"entry_exit_2x2_trajectory_period_incomplete:{policy}")

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
    checkpoint_path = args.output_dir / "economic-2x2-checkpoint.json"
    if checkpoint_path.exists():
        checkpoint = _read_json(checkpoint_path)
        if "trajectories" in checkpoint:
            if checkpoint["trajectories"] != trajectories:
                raise ValueError("entry_exit_2x2_checkpoint_lineage_mismatch")
            simulated: dict[str, dict[str, Mapping[str, Any]]] = checkpoint.get("results", {})
        elif checkpoint.get("trajectory_hash") == args.x1_5_d3_trajectory_hash:
            simulated = {"x1.5_exit_d3": checkpoint.get("results", {})}
        else:
            raise ValueError("entry_exit_2x2_checkpoint_lineage_mismatch")
    else:
        simulated = {}
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
        policy_results = simulated.setdefault(policy, {})
        for index, level in enumerate(GROUP_LEVELS, start=1):
            if level in policy_results:
                print(
                    f"entry-exit 2x2: {policy} {index}/{len(GROUP_LEVELS)} " f"{level} reused=1",
                    flush=True,
                )
                continue
            parameters = _candidate_parameters(level, growth_multiplier="1.5")
            representation = RepresentationMinimumLookup(
                eligibility_masks=masks,
                bit=bit_by_variant[(8, level)],
                spike_keys=spike_keys,
            )
            result = _simulate(
                inputs=inputs,
                scenario=_scenario_for_candidate(
                    base_scenario,
                    candidate_id=f"{policy}-{level}",
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
            policy_results[level] = metrics
            _write_json(
                checkpoint_path,
                {
                    "schema": "assortment_lifecycle_entry_exit_2x2_checkpoint.v2",
                    "dataset_hash": args.dataset_hash,
                    "trajectories": trajectories,
                    "results": simulated,
                },
            )
            print(
                f"entry-exit 2x2: {policy} {index}/{len(GROUP_LEVELS)} " f"{level} {metrics}",
                flush=True,
            )
            del result
            gc.collect()

    rows = combine_2x2_results(
        x1_2_rows=x1_2_grid["results"],
        x1_5_base=simulated["x1.5_base"],
        x1_5_d3=simulated["x1.5_exit_d3"],
    )
    compact_reconciliation = {
        level: {
            metric: str(
                Decimal(str(simulated["x1.5_base"][level][metric]))
                - compact_x1_5_base[level][metric]
            )
            for metric in DECISION_METRICS
        }
        for level in GROUP_LEVELS
    }
    payload = {
        "schema": "assortment_lifecycle_entry_exit_2x2_economics.v1",
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
            for policy, row in trajectory_rows.items()
        },
        "application": application,
        "methodology": {
            "profiles": ["x1.2_base", "x1.2_exit_d3", "x1.5_base", "x1.5_exit_d3"],
            "x1.2_rows": "reused_validated_duration_grid",
            "x1.5_base": "fresh_memory_safe_simulation",
            "x1.5_exit_d3": "fresh_memory_safe_simulation_reused_from_checkpoint_if_present",
            "compact_x1.5_base": "reconciliation_only_not_used_in_2x2_comparison",
            "compact_capital_reconstruction": (
                "gross_profit_delta - economic_effect_delta, annual carry rate 0.65, "
                "150 training days"
            ),
            "economic_holdout": "not_consumed",
            "look_ahead_free": True,
        },
        "compact_x1.5_base_reconciliation_fresh_minus_existing": compact_reconciliation,
        "results": rows,
        "holdout_consumed": False,
        "production_authorized": False,
        "production_action": "none_read_only",
    }
    _write_json(args.output_dir / "economic-2x2.json", payload)
    print(json.dumps({"status": payload["status"], "rows": len(rows)}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
