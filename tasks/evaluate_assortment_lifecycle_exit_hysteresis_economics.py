"""Evaluate exit hysteresis on the frozen lifecycle-v2 economic training window.

The task reuses immutable lifecycle trajectories and the existing frozen auto-order
simulation.  It never rebuilds source facts, consumes the July economic holdout, or
performs production/external writes.
"""

from __future__ import annotations

import argparse
import csv
import gc
import hashlib
import json
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, MutableMapping

from app.services.assortment_lifecycle_replay_store import (
    AssortmentLifecycleReplayStore,
)
from app.services.assortment_lifecycle_v2_policy import (
    DEFAULT_ASSORTMENT_LIFECYCLE_V2_POLICY_PATH,
    load_assortment_lifecycle_v2_policy,
)
from tasks.build_display_auto_order_dry_run import load_auto_order_policy
from tasks.display_auto_order_backtest_preflight import (
    load_scenario_config,
    validate_preflight_directory,
)
from tasks.report_display_auto_order_frozen_backtest import (
    _clean,
    _load_scenarios,
)
from tasks.run_assortment_lifecycle_v2_economic_backtest import (
    GROUP_LEVELS,
    RepresentationMinimumLookup,
    _candidate_id,
    _load_item_group_keys,
    _period_metrics,
    _scenario_for_candidate,
    _simulate,
    _soft_rate,
    build_representation_masks,
    load_frozen_inputs,
)

DEFAULT_DATASET_HASH = "582e10da08e6968f5e1aa450cd88df5dfb5af6c2b9ba84ad05799ec6ec17a6d1"
DEFAULT_BASE_TRAJECTORY_HASH = "1e744d385bdf04fa2066bcc7ed6590e2b4e2a98ea7c1743266a31ea03e7a7a49"
DEFAULT_HYSTERESIS_TRAJECTORY_HASH = (
    "dfa7437dcdc444562926356a545947580b7dc297c6584594a21a5ab71b83b673"
)
DEFAULT_CONTROL_SCENARIO_ID = "grow_cap20_p90_hold4_typical_kmp0_5_sitebalanced_base"
DEFAULT_PREFLIGHT_DIR = Path(
    "reports/assortment_lifecycle/backtest-2026-01-01_2026-07-31/preflight"
)
DEFAULT_EXISTING_ECONOMIC_DIR = Path(
    "reports/assortment_lifecycle/backtest-2026-01-01_2026-07-31/"
    "assortment-lifecycle-v2-economic-backtest"
)
DEFAULT_OUTPUT_DIR = Path(
    "reports/assortment_lifecycle/backtest-2026-01-01_2026-07-31/"
    "assortment-lifecycle-v2-exit-hysteresis-d7-2026-08-15-v1"
)
DEFAULT_REPLAY_STORE = Path(".local/assortment-lifecycle-backtest-store.sqlite3")
MONEY_AND_QUANTITY_METRICS = (
    "served_sales_qty",
    "gross_profit_rub",
    "average_inventory_value_rub",
    "carrying_cost_rub",
    "economic_effect_rub",
    "gmroi",
    "ending_inventory_qty",
    "ending_target_stock_qty",
    "ending_excess_stock_qty",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def apply_stored_trajectory(
    *,
    store: AssortmentLifecycleReplayStore,
    trajectory_hash: str,
    fact_by_key: MutableMapping[tuple[date, str], dict[str, str]],
    date_to: date,
) -> tuple[str, int, int, set[tuple[date, str]], dict[tuple[date, str], Decimal]]:
    """Stream one verified trajectory into frozen facts through ``date_to``."""

    digest = hashlib.sha256()
    applied_count = 0
    missing_fact_count = 0
    spike_keys: set[tuple[date, str]] = set()
    spike_rates: dict[tuple[date, str], Decimal] = {}
    for row in store.iter_trajectory_rows(trajectory_hash):
        business_date = date.fromisoformat(_clean(row.get("business_date")))
        if business_date > date_to:
            # Continue consuming the iterator so its immutable checksum is verified.
            continue
        code = _clean(row.get("nomenclature_code"))
        status = _clean(row.get("status"))
        key = (business_date, code)
        fact = fact_by_key.get(key)
        if fact is None:
            missing_fact_count += 1
            continue
        previous_status = _clean(row.get("previous_status")) or status
        fact["previous_status"] = previous_status
        fact["status"] = status
        demand_state = _clean(row.get("demand_state"))
        digest.update(
            f"{business_date.isoformat()}\0{code}\0{previous_status}\0{status}\0"
            f"{demand_state}\n".encode()
        )
        applied_count += 1
        if demand_state == "spike":
            spike_keys.add(key)
            rate = Decimal(
                str(
                    _soft_rate(
                        float(row.get("sales_30") or 0),
                        30,
                        (
                            float(row["available_days_30"])
                            if row.get("available_days_30") not in (None, "")
                            else None
                        ),
                    )
                )
            )
            if rate > 0:
                spike_rates[key] = rate
    return digest.hexdigest(), applied_count, missing_fact_count, spike_keys, spike_rates


def metric_deltas(candidate: Mapping[str, Any], baseline: Mapping[str, Any]) -> dict[str, str]:
    return {
        f"{metric}_delta": str(_decimal(candidate[metric]) - _decimal(baseline[metric]))
        for metric in MONEY_AND_QUANTITY_METRICS
    }


def _candidate_parameters(
    group_level: str,
    *,
    growth_multiplier: str = "1.2",
) -> dict[str, Any]:
    return {
        "growth_multiplier": growth_multiplier,
        "confirmation_days": 14,
        "max_single_day_share": "0.7",
        "min_independent_sales": 2,
        "spike_quantity_policy": "ordinary_demand_only",
        "comparable_group_min_size": 8,
        "comparable_group_level": group_level,
    }


def _trajectory_manifest_row(
    store: AssortmentLifecycleReplayStore, trajectory_hash: str
) -> Mapping[str, Any]:
    rows = [
        row for row in store.manifest()["trajectories"] if row["trajectory_hash"] == trajectory_hash
    ]
    if len(rows) != 1:
        raise ValueError(f"economic_trajectory_not_found:{trajectory_hash}")
    return rows[0]


def _existing_base_candidates(
    training_results: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    by_level: dict[str, Mapping[str, Any]] = {}
    for row in training_results.get("candidates", []):
        parameters = row.get("parameters", {})
        if all(
            parameters.get(key) == value
            for key, value in _candidate_parameters(
                str(parameters.get("comparable_group_level", ""))
            ).items()
        ):
            by_level[str(parameters["comparable_group_level"])] = row
    if set(by_level) != set(GROUP_LEVELS):
        raise ValueError("economic_existing_x1_2_candidates_incomplete")
    return by_level


def _write_csv(path: Path, rows: list[Mapping[str, Any]]) -> None:
    fields = [
        "policy",
        "comparable_group_level",
        *MONEY_AND_QUANTITY_METRICS,
        *(f"vs_legacy_{metric}_delta" for metric in MONEY_AND_QUANTITY_METRICS),
        *(f"vs_base_{metric}_delta" for metric in MONEY_AND_QUANTITY_METRICS),
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _load_checkpoint(
    path: Path,
    *,
    dataset_hash: str,
    base_trajectory_hash: str,
    hysteresis_trajectory_hash: str,
) -> dict[str, dict[str, Mapping[str, Any]]]:
    if not path.exists():
        return {}
    payload = _json(path)
    expected = {
        "dataset_hash": dataset_hash,
        "base_trajectory_hash": base_trajectory_hash,
        "hysteresis_trajectory_hash": hysteresis_trajectory_hash,
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise ValueError("economic_checkpoint_lineage_mismatch")
    return {
        str(policy): {str(level): metrics for level, metrics in levels.items()}
        for policy, levels in payload.get("results", {}).items()
    }


def _write_checkpoint(
    path: Path,
    *,
    dataset_hash: str,
    base_trajectory_hash: str,
    hysteresis_trajectory_hash: str,
    results: Mapping[str, Mapping[str, Mapping[str, Any]]],
) -> None:
    _write_json(
        path,
        {
            "schema": "assortment_lifecycle_exit_hysteresis_economic_checkpoint.v1",
            "dataset_hash": dataset_hash,
            "base_trajectory_hash": base_trajectory_hash,
            "hysteresis_trajectory_hash": hysteresis_trajectory_hash,
            "results": results,
        },
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight-dir", type=Path, default=DEFAULT_PREFLIGHT_DIR)
    parser.add_argument("--existing-economic-dir", type=Path, default=DEFAULT_EXISTING_ECONOMIC_DIR)
    parser.add_argument("--replay-store-path", type=Path, default=DEFAULT_REPLAY_STORE)
    parser.add_argument("--dataset-hash", default=DEFAULT_DATASET_HASH)
    parser.add_argument("--base-trajectory-hash", default=DEFAULT_BASE_TRAJECTORY_HASH)
    parser.add_argument("--hysteresis-trajectory-hash", default=DEFAULT_HYSTERESIS_TRAJECTORY_HASH)
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
    store = AssortmentLifecycleReplayStore(args.replay_store_path)
    trajectory_rows = {
        "x1.2_base": _trajectory_manifest_row(store, args.base_trajectory_hash),
        "x1.2_exit_d7": _trajectory_manifest_row(store, args.hysteresis_trajectory_hash),
    }
    for name, row in trajectory_rows.items():
        if row["dataset_hash"] != args.dataset_hash:
            raise ValueError(f"economic_trajectory_dataset_mismatch:{name}")
        if (
            row["period_from"] > "2026-01-01"
            or row["period_to"] < policy_v2.periods.training_to.isoformat()
        ):
            raise ValueError(f"economic_trajectory_period_incomplete:{name}")

    baseline_path = args.existing_economic_dir / "training-legacy-baseline.json"
    training_results_path = args.existing_economic_dir / "training-results.json"
    legacy_baseline = _json(baseline_path)
    existing_candidates = _existing_base_candidates(_json(training_results_path))
    run_manifest = {
        "schema": "assortment_lifecycle_exit_hysteresis_economic_run.v1",
        "status": "running_training",
        "dataset_hash": args.dataset_hash,
        "training_period_from": policy_v2.periods.training_from.isoformat(),
        "training_period_to": policy_v2.periods.training_to.isoformat(),
        "warmup_from": "2026-01-01",
        "base_trajectory_hash": args.base_trajectory_hash,
        "hysteresis_trajectory_hash": args.hysteresis_trajectory_hash,
        "holdout_consumed": False,
        "production_authorized": False,
        "production_action": "none_read_only",
    }
    _write_json(args.output_dir / "economic-run-manifest.json", run_manifest)

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
    shared_demand_sample_cache: dict[tuple[str, date, int], list[Decimal]] = {}
    checkpoint_path = args.output_dir / "economic-simulation-checkpoint.json"
    results = _load_checkpoint(
        checkpoint_path,
        dataset_hash=args.dataset_hash,
        base_trajectory_hash=args.base_trajectory_hash,
        hysteresis_trajectory_hash=args.hysteresis_trajectory_hash,
    )
    application: dict[str, Any] = {}
    for policy_name, trajectory_hash in (
        ("x1.2_base", args.base_trajectory_hash),
        ("x1.2_exit_d7", args.hysteresis_trajectory_hash),
    ):
        signature, applied, missing, spike_keys, spike_rates = apply_stored_trajectory(
            store=store,
            trajectory_hash=trajectory_hash,
            fact_by_key=inputs.fact_by_key,
            date_to=policy_v2.periods.training_to,
        )
        application[policy_name] = {
            "training_trajectory_signature": signature,
            "applied_row_count": applied,
            "trajectory_rows_outside_economic_fact_population": missing,
            "spike_row_count": len(spike_keys),
        }
        policy_results = results.setdefault(policy_name, {})
        for index, group_level in enumerate(GROUP_LEVELS, start=1):
            if group_level in policy_results:
                print(
                    f"economic training: {policy_name} {index}/{len(GROUP_LEVELS)} "
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
            scenario = _scenario_for_candidate(
                base_scenario,
                candidate_id=f"{policy_name}-{_candidate_id(parameters)}",
                parameters=parameters,
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
            metrics = _period_metrics(
                result,
                period_from=policy_v2.periods.training_from,
                period_to=policy_v2.periods.training_to,
            )
            policy_results[group_level] = metrics
            _write_checkpoint(
                checkpoint_path,
                dataset_hash=args.dataset_hash,
                base_trajectory_hash=args.base_trajectory_hash,
                hysteresis_trajectory_hash=args.hysteresis_trajectory_hash,
                results=results,
            )
            print(
                f"economic training: {policy_name} {index}/{len(GROUP_LEVELS)} "
                f"{group_level} {metrics}",
                flush=True,
            )
            del result
            gc.collect()
    legacy_metrics = legacy_baseline["metrics"]
    reconciliation: dict[str, Any] = {}
    rows: list[dict[str, Any]] = []
    for group_level in GROUP_LEVELS:
        base_metrics = results["x1.2_base"][group_level]
        expected = existing_candidates[group_level]["metrics"]
        observed_limited = {
            "served_sales_delta_qty": metric_deltas(base_metrics, legacy_metrics)[
                "served_sales_qty_delta"
            ],
            "gross_profit_delta_rub": metric_deltas(base_metrics, legacy_metrics)[
                "gross_profit_rub_delta"
            ],
            "economic_effect_delta_rub": metric_deltas(base_metrics, legacy_metrics)[
                "economic_effect_rub_delta"
            ],
            "gmroi_delta": metric_deltas(base_metrics, legacy_metrics)["gmroi_delta"],
            "ending_excess_stock_delta_qty": metric_deltas(base_metrics, legacy_metrics)[
                "ending_excess_stock_qty_delta"
            ],
        }
        differences = {
            key: str(_decimal(observed_limited[key]) - _decimal(expected[key]))
            for key in expected
            if observed_limited[key] != expected[key]
        }
        matches = not differences
        reconciliation[group_level] = {
            "matches_existing_training_result": matches,
            "candidate_id": existing_candidates[group_level]["candidate_id"],
            "differences_fresh_immutable_minus_existing": differences,
        }
        for policy_name in ("x1.2_base", "x1.2_exit_d7"):
            metrics = results[policy_name][group_level]
            row = {
                "policy": policy_name,
                "comparable_group_level": group_level,
                **{metric: str(metrics[metric]) for metric in MONEY_AND_QUANTITY_METRICS},
                **{
                    f"vs_legacy_{key}": value
                    for key, value in metric_deltas(metrics, legacy_metrics).items()
                },
                **{
                    f"vs_base_{key}": value
                    for key, value in metric_deltas(metrics, base_metrics).items()
                },
            }
            rows.append(row)

    comparison = {
        "schema": "assortment_lifecycle_exit_hysteresis_economic_comparison.v1",
        "status": "training_complete_no_production_decision",
        "period": {
            "warmup_from": "2026-01-01",
            "training_from": policy_v2.periods.training_from.isoformat(),
            "training_to": policy_v2.periods.training_to.isoformat(),
        },
        "lineage": {
            "dataset_hash": args.dataset_hash,
            "preflight_manifest_sha256": _sha256(args.preflight_dir / "run-manifest.json"),
            "policy_sha256": _sha256(args.policy_json),
            "auto_order_policy_sha256": _sha256(args.auto_order_policy_json),
            "scenario_config_sha256": _sha256(args.scenario_config_json),
            "legacy_baseline_sha256": _sha256(baseline_path),
            "existing_training_results_sha256": _sha256(training_results_path),
            "trajectories": {
                name: {
                    "trajectory_hash": row["trajectory_hash"],
                    "content_sha256": row["content_sha256"],
                    "row_count": row["row_count"],
                }
                for name, row in trajectory_rows.items()
            },
            "application": application,
            "look_ahead_free": True,
        },
        "methodology": {
            "stage_profiles": ["x1.2_base", "x1.2_exit_d7"],
            "entry_rule": "x1.2_with_14_day_confirmation_inherited_from_base",
            "exit_rule": "base_x1.2_working_requested_for_7_consecutive_days",
            "spike_quantity_policy": "ordinary_demand_only",
            "comparable_group_min_size": 8,
            "comparable_group_levels": list(GROUP_LEVELS),
            "economic_holdout": "not_consumed",
        },
        "legacy_baseline": legacy_baseline,
        "results": rows,
        "base_reconciliation": reconciliation,
        "holdout_consumed": False,
        "production_authorized": False,
        "production_action": "none_read_only",
    }
    _write_json(args.output_dir / "economic-comparison.json", comparison)
    _write_csv(args.output_dir / "economic-comparison.csv", rows)
    run_manifest.update(
        {
            "status": "training_complete_no_production_decision",
            "result_sha256": _sha256(args.output_dir / "economic-comparison.json"),
            "result_row_count": len(rows),
            "base_reconciliation_passed": all(
                row["matches_existing_training_result"] for row in reconciliation.values()
            ),
        }
    )
    _write_json(args.output_dir / "economic-run-manifest.json", run_manifest)
    print(json.dumps(run_manifest, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
