"""Test the last actionable first-day boundary growth-order component on training.

The x1.2 e1+d3 lifecycle trajectory remains immutable.  For temporally
validated risky boundary-entry dates, the candidate removes the representation
minimum, which is the only independently switchable growth quantity left after
ordinary demand and safety stock are preserved.  Fresh paired controls avoid
comparing against path-dependent metrics from an older run.  July and all
production/external actions remain closed.
"""

from __future__ import annotations

import argparse
import csv
import gc
import json
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping

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
from tasks.evaluate_assortment_lifecycle_boundary_quantity_guardrail import (
    DEFAULT_BASELINE_TRAJECTORY_HASH,
    DEFAULT_RISK_DIAGNOSTIC,
    GuardedRepresentationMinimumLookup,
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

DEFAULT_OUTPUT_DIR = Path(
    "reports/assortment_lifecycle/backtest-2026-01-01_2026-07-31/"
    "boundary-entry-incremental-growth-guardrail-x1.2-2026-08-15-v2"
)
DEFAULT_OUTPUT = DEFAULT_OUTPUT_DIR / "incremental-growth-guardrail.json"
DEFAULT_CHECKPOINT = DEFAULT_OUTPUT_DIR / "incremental-growth-guardrail-checkpoint.json"
DEFAULT_DETAIL_CSV = DEFAULT_OUTPUT_DIR / "guarded-key-order-comparison.csv"
MEANINGFUL_ECONOMIC_EFFECT_RUB = Decimal("30000")
DECISION_FIELDS = (
    "decision_date",
    "nomenclature_code",
    "status",
    "grow_protection_reason",
    "unprotected_min_stock_qty",
    "unprotected_max_stock_qty",
    "min_stock_qty",
    "max_stock_qty",
    "economic_safety_stock_qty",
    "representation_minimum_qty",
    "decision_service_buffer_qty",
    "acceleration_order_component_qty",
    "ordinary_recommended_order_qty",
)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _decision_slice(
    rows: Iterable[Mapping[str, Any]],
    *,
    guarded_keys: set[tuple[date, str]],
) -> list[dict[str, Any]]:
    selected: dict[tuple[date, str], dict[str, Any]] = {}
    for row in rows:
        key = (
            date.fromisoformat(str(row["decision_date"])),
            str(row["nomenclature_code"]),
        )
        if key not in guarded_keys:
            continue
        if key in selected:
            raise ValueError(f"incremental_growth_duplicate_decision:{key}")
        selected[key] = {field: row.get(field, "") for field in DECISION_FIELDS}
    return [selected[key] for key in sorted(selected)]


def _dec(value: Any) -> Decimal:
    return Decimal(str(value or 0))


def summarize_order_pair(
    *,
    guarded_keys: set[tuple[date, str]],
    control_rows: Iterable[Mapping[str, Any]],
    candidate_rows: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    def by_key(rows: Iterable[Mapping[str, Any]]) -> dict[tuple[str, str], Mapping[str, Any]]:
        return {(str(row["decision_date"]), str(row["nomenclature_code"])): row for row in rows}

    control = by_key(control_rows)
    candidate = by_key(candidate_rows)
    expected = {(business_date.isoformat(), code) for business_date, code in guarded_keys}
    details: list[dict[str, Any]] = []
    changed_count = 0
    reduced_qty = Decimal("0")
    increased_qty = Decimal("0")
    control_total = Decimal("0")
    candidate_total = Decimal("0")
    representation_binding_count = 0
    acceleration_component_total = Decimal("0")
    decision_service_buffer_total = Decimal("0")
    protection_increment_total = Decimal("0")
    safety_stock_total = Decimal("0")

    for key in sorted(expected):
        control_row = control.get(key, {})
        candidate_row = candidate.get(key, {})
        control_order = _dec(control_row.get("ordinary_recommended_order_qty"))
        candidate_order = _dec(candidate_row.get("ordinary_recommended_order_qty"))
        order_delta = candidate_order - control_order
        control_total += control_order
        candidate_total += candidate_order
        changed_count += int(order_delta != 0)
        reduced_qty += max(Decimal("0"), -order_delta)
        increased_qty += max(Decimal("0"), order_delta)

        raw_min = _dec(control_row.get("unprotected_min_stock_qty"))
        raw_max = _dec(control_row.get("unprotected_max_stock_qty"))
        final_min = _dec(control_row.get("min_stock_qty"))
        final_max = _dec(control_row.get("max_stock_qty"))
        representation = _dec(control_row.get("representation_minimum_qty"))
        decision_buffer = _dec(control_row.get("decision_service_buffer_qty"))
        expected_min_without_protection = max(raw_min, representation) + decision_buffer
        expected_max_without_protection = max(raw_max, representation) + decision_buffer
        protection_increment = max(
            Decimal("0"),
            final_min - expected_min_without_protection,
            final_max - expected_max_without_protection,
        )
        protection_increment_total += protection_increment
        representation_binding_count += int(representation > raw_min or representation > raw_max)
        acceleration_component_total += _dec(control_row.get("acceleration_order_component_qty"))
        decision_service_buffer_total += decision_buffer
        safety_stock_total += _dec(control_row.get("economic_safety_stock_qty"))
        details.append(
            {
                "business_date": key[0],
                "nomenclature_code": key[1],
                "control_order_qty": str(control_order),
                "candidate_order_qty": str(candidate_order),
                "order_qty_delta": str(order_delta),
                "control_representation_minimum_qty": str(representation),
                "control_safety_stock_qty": str(_dec(control_row.get("economic_safety_stock_qty"))),
                "control_growth_protection_increment_qty": str(protection_increment),
                "control_acceleration_order_component_qty": str(
                    _dec(control_row.get("acceleration_order_component_qty"))
                ),
                "control_decision_service_buffer_qty": str(decision_buffer),
            }
        )

    return {
        "guarded_key_count": len(expected),
        "control_decision_coverage_count": len(expected & set(control)),
        "candidate_decision_coverage_count": len(expected & set(candidate)),
        "changed_order_key_count": changed_count,
        "control_order_qty": str(control_total),
        "candidate_order_qty": str(candidate_total),
        "reduced_order_qty": str(reduced_qty),
        "increased_order_qty": str(increased_qty),
        "representation_binding_target_key_count": representation_binding_count,
        "preserved_safety_stock_qty": str(safety_stock_total),
        "growth_protection_increment_qty": str(protection_increment_total),
        "acceleration_order_component_qty": str(acceleration_component_total),
        "decision_service_buffer_qty": str(decision_service_buffer_total),
        "details": details,
    }


def meaningful_guardrails(
    candidate: Mapping[str, Any],
    control: Mapping[str, Any],
) -> dict[str, bool]:
    deltas = {
        metric: _dec(candidate[metric]) - _dec(control[metric])
        for metric in MONEY_AND_QUANTITY_METRICS
    }
    return {
        "served_sales_not_worse": deltas["served_sales_qty"] >= 0,
        "gross_profit_not_worse": deltas["gross_profit_rub"] >= 0,
        "economic_effect_gain_at_least_30000_rub": (
            deltas["economic_effect_rub"] >= MEANINGFUL_ECONOMIC_EFFECT_RUB
        ),
        "gmroi_not_worse": deltas["gmroi"] >= 0,
        "ending_excess_not_worse": deltas["ending_excess_stock_qty"] <= 0,
    }


def _write_detail_csv(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    payload = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        fields = list(payload[0]) if payload else []
        writer = csv.DictWriter(handle, fieldnames=fields)
        if fields:
            writer.writeheader()
            writer.writerows(payload)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight-dir", type=Path, default=DEFAULT_PREFLIGHT_DIR)
    parser.add_argument("--replay-store-path", type=Path, default=DEFAULT_REPLAY_STORE)
    parser.add_argument("--dataset-hash", default=DEFAULT_DATASET_HASH)
    parser.add_argument("--baseline-trajectory-hash", default=DEFAULT_BASELINE_TRAJECTORY_HASH)
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
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--checkpoint", type=Path, default=DEFAULT_CHECKPOINT)
    parser.add_argument("--detail-csv", type=Path, default=DEFAULT_DETAIL_CSV)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    policy_v2 = load_assortment_lifecycle_v2_policy(args.policy_json)
    validate_preflight_directory(args.preflight_dir)
    diagnostic = _read_json(args.risk_diagnostic)
    if diagnostic["dataset_hash"] != args.dataset_hash:
        raise ValueError("incremental_growth_diagnostic_dataset_mismatch")
    if diagnostic["validation_gates"]["passed"] is not True:
        raise ValueError("incremental_growth_temporal_validation_required")
    if diagnostic["july_holdout_consumed"] is not False:
        raise ValueError("incremental_growth_diagnostic_holdout_consumed")
    guarded_keys = {
        (date.fromisoformat(row["business_date"]), str(row["nomenclature_code"]))
        for row in diagnostic["selected_training_keys"]
    }

    store = AssortmentLifecycleReplayStore(args.replay_store_path)
    trajectory = _trajectory_manifest_row(store, args.baseline_trajectory_hash)
    if trajectory["dataset_hash"] != args.dataset_hash:
        raise ValueError("incremental_growth_trajectory_dataset_mismatch")
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
    if base_scenario.grow_acceleration_profile != "off":
        raise ValueError("incremental_growth_acceleration_must_be_off")
    if base_scenario.grow_service_floor_percentile != 0:
        raise ValueError("incremental_growth_service_floor_must_be_off")
    group_keys = _load_item_group_keys(args.replay_store_path, dataset_hash=args.dataset_hash)
    masks, bit_by_variant = build_representation_masks(
        inputs=inputs,
        group_keys_by_code=group_keys,
        group_sizes=(8,),
        group_levels=GROUP_LEVELS,
    )
    checkpoint = (
        _read_json(args.checkpoint)
        if args.checkpoint.exists()
        else {
            "schema": "boundary_incremental_growth_checkpoint.v1",
            "dataset_hash": args.dataset_hash,
            "trajectory_hash": args.baseline_trajectory_hash,
            "guarded_key_count": len(guarded_keys),
            "levels": {},
        }
    )
    if checkpoint["dataset_hash"] != args.dataset_hash:
        raise ValueError("incremental_growth_checkpoint_dataset_mismatch")
    if checkpoint["trajectory_hash"] != args.baseline_trajectory_hash:
        raise ValueError("incremental_growth_checkpoint_trajectory_mismatch")
    if checkpoint["guarded_key_count"] != len(guarded_keys):
        raise ValueError("incremental_growth_checkpoint_key_count_mismatch")
    shared_demand_sample_cache: dict[tuple[str, date, int], list[Decimal]] = {}

    def run(level: str, *, candidate: bool) -> dict[str, Any]:
        representation_base = RepresentationMinimumLookup(
            eligibility_masks=masks,
            bit=bit_by_variant[(8, level)],
            spike_keys=spike_keys,
        )
        representation = (
            GuardedRepresentationMinimumLookup(
                base=representation_base,
                guarded_keys=guarded_keys,
                cap_qty=Decimal("0"),
            )
            if candidate
            else representation_base
        )
        role = "candidate-no-increment" if candidate else "paired-control"
        result = _simulate(
            inputs=inputs,
            scenario=_scenario_for_candidate(
                base_scenario,
                candidate_id=f"boundary-risk-{role}-{level}",
                parameters=_candidate_parameters(level, growth_multiplier="1.2"),
            ),
            policy=auto_order_policy,
            config=scenario_config,
            date_from=date(2026, 1, 1),
            date_to=policy_v2.periods.training_to,
            representation_minimums=representation,
            spike_keys=spike_keys,
            spike_rates=spike_rates,
            demand_sample_cache=shared_demand_sample_cache,
            keep_decision_detail=True,
        )
        payload = {
            "metrics": {
                metric: str(value)
                for metric, value in _period_metrics(
                    result,
                    period_from=policy_v2.periods.training_from,
                    period_to=policy_v2.periods.training_to,
                ).items()
            },
            "decisions": _decision_slice(result.decision_rows, guarded_keys=guarded_keys),
        }
        del result
        gc.collect()
        return payload

    for level in GROUP_LEVELS:
        level_result = checkpoint["levels"].setdefault(level, {})
        for role, is_candidate in (("control", False), ("candidate", True)):
            if role in level_result:
                continue
            level_result[role] = run(level, candidate=is_candidate)
            _write_json(args.checkpoint, checkpoint)
            print(
                json.dumps(
                    {"completed_level": level, "completed_role": role},
                    ensure_ascii=False,
                ),
                flush=True,
            )

    result_rows = []
    detail_rows = []
    all_levels_passed = True
    actionable_component_found = False
    for level in GROUP_LEVELS:
        control = checkpoint["levels"][level]["control"]
        candidate = checkpoint["levels"][level]["candidate"]
        order_summary = summarize_order_pair(
            guarded_keys=guarded_keys,
            control_rows=control["decisions"],
            candidate_rows=candidate["decisions"],
        )
        guardrails = meaningful_guardrails(candidate["metrics"], control["metrics"])
        passed = all(guardrails.values())
        all_levels_passed = all_levels_passed and passed
        actionable_component_found = actionable_component_found or (
            Decimal(order_summary["reduced_order_qty"]) > 0
        )
        result_rows.append(
            {
                "comparable_group_level": level,
                **{metric: candidate["metrics"][metric] for metric in MONEY_AND_QUANTITY_METRICS},
                **metric_deltas(candidate["metrics"], control["metrics"]),
                "guardrails": guardrails,
                "passed": passed,
                "order_component": {
                    key: value for key, value in order_summary.items() if key != "details"
                },
            }
        )
        detail_rows.extend(
            {"comparable_group_level": level, **row} for row in order_summary["details"]
        )

    status = (
        "training_candidate_passed_all_group_levels"
        if all_levels_passed and actionable_component_found
        else (
            "no_actionable_incremental_component_direction_closed"
            if not actionable_component_found
            else "meaningful_economic_gate_failed_direction_closed"
        )
    )
    payload = {
        "schema": "assortment_lifecycle_boundary_incremental_growth_guardrail.v1",
        "status": status,
        "period": {
            "warmup_from": "2026-01-01",
            "training_from": policy_v2.periods.training_from.isoformat(),
            "training_to": policy_v2.periods.training_to.isoformat(),
        },
        "dataset_hash": args.dataset_hash,
        "trajectory": {
            "trajectory_hash": trajectory["trajectory_hash"],
            "content_sha256": trajectory["content_sha256"],
            "row_count": trajectory["row_count"],
            "training_signature": signature,
            "applied_row_count": applied,
            "rows_outside_economic_fact_population": missing,
        },
        "risk_rule": {
            "rule_id": diagnostic["selection"]["selected_rule_id"],
            "guarded_training_key_count": len(guarded_keys),
            "validation_metrics": diagnostic["validation_selected_rule_metrics"],
        },
        "component_model": {
            "ordinary_demand_preserved": True,
            "safety_stock_preserved": True,
            "lifecycle_stage_preserved": True,
            "strong_x1.5_entries_preserved": True,
            "grow_acceleration_profile": base_scenario.grow_acceleration_profile,
            "grow_service_floor_percentile": str(base_scenario.grow_service_floor_percentile),
            "grow_entry_protection_weeks": base_scenario.grow_entry_protection_weeks,
            "grow_weekly_reduction_cap": str(base_scenario.grow_weekly_reduction_cap),
            "candidate_action": (
                "remove the residual representation minimum on risky first-day entries"
            ),
            "candidate_is_maximum_residual_component_removal": True,
        },
        "meaningful_gate": {
            "economic_effect_gain_rub": str(MEANINGFUL_ECONOMIC_EFFECT_RUB),
            "served_sales_not_worse": True,
            "gross_profit_not_worse": True,
            "gmroi_not_worse": True,
            "ending_excess_not_worse": True,
            "all_group_levels_required": True,
        },
        "results": result_rows,
        "all_group_levels_passed": all_levels_passed,
        "actionable_component_found": actionable_component_found,
        "holdout_consumed": False,
        "production_authorized": False,
        "production_action": "none_read_only",
    }
    _write_json(args.output, payload)
    _write_detail_csv(args.detail_csv, detail_rows)
    print(
        json.dumps(
            {
                "status": status,
                "actionable_component_found": actionable_component_found,
                "all_group_levels_passed": all_levels_passed,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
