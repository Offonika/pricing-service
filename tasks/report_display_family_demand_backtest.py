"""Run a read-only family-demand challenger on frozen display backtest facts.

The challenger preserves the latest known total forecast of each compatible
family and only reallocates that rate between quality/construction segments and
SKU using sales completed before the decision date.  It never writes orders,
1C, lifecycle status, or production data.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from collections import defaultdict
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, MutableMapping, Sequence

from app.services.assortment_lifecycle_v2_policy import (
    DEFAULT_ASSORTMENT_LIFECYCLE_V2_POLICY_PATH,
    load_assortment_lifecycle_v2_policy,
)
from app.services.display_family_demand import (
    DisplayFamilyMember,
    allocate_display_family_rates,
    build_display_family_members,
)
from tasks.build_display_auto_order_dry_run import (
    _analog_model_tokens,
    load_auto_order_policy,
)
from tasks.display_auto_order_backtest_preflight import (
    load_scenario_config,
    validate_preflight_directory,
)
from tasks.report_display_auto_order_frozen_backtest import (
    _clean,
    _decimal,
    _load_scenarios,
)
from tasks.run_assortment_lifecycle_v2_economic_backtest import (
    DEFAULT_CONTROL_SCENARIO_ID,
    DEFAULT_DATASET_HASH,
    DEFAULT_PREFLIGHT_DIR,
    DEFAULT_REPLAY_DIR,
    DEFAULT_REPLAY_STORE,
    FrozenInputs,
    RepresentationMinimumLookup,
    _deltas,
    _load_item_group_keys,
    _period_metrics,
    _profile,
    _scenario_for_candidate,
    _simulate,
    apply_v2_profile,
    build_representation_masks,
    load_frozen_inputs,
)

ZERO = Decimal("0")
DEFAULT_FOCUS_CODE = "РБ000063532"
DEFAULT_LOOKBACK_DAYS = 14
DEFAULT_BLEND = Decimal("0.5")
DEFAULT_CANDIDATE_SUMMARY = Path(
    "reports/assortment_lifecycle/backtest-2026-01-01_2026-07-31/"
    "fact-vs-legacy-vs-v2-business-report-2026-08-14/working-shortfall-summary.json"
)
DEFAULT_OUTPUT_DIR = Path(
    "reports/assortment_lifecycle/backtest-2026-01-01_2026-07-31/"
    "display-family-demand-backtest-2026-08-15"
)
METRIC_KEYS = (
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


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _read_metrics(path: Path) -> dict[str, Decimal]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {key: _decimal(payload.get(key)) for key in METRIC_KEYS}


def _run_fingerprint(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _artifacts_match(output_dir: Path, expected: Mapping[str, str]) -> bool:
    return bool(expected) and all(
        (output_dir / name).is_file() and _sha256(output_dir / name) == digest
        for name, digest in expected.items()
    )


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = sorted({str(key) for row in rows for key in row})
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)


def build_family_members_from_decisions(
    decision_rows_by_date: Mapping[date, Sequence[Mapping[str, Any]]],
) -> tuple[dict[str, DisplayFamilyMember], dict[str, date]]:
    first_seen: dict[str, date] = {}
    identities: dict[str, dict[str, Any]] = {}
    for business_date in sorted(decision_rows_by_date):
        for row in decision_rows_by_date[business_date]:
            code = _clean(row.get("nomenclature_code"))
            if not code or code in identities:
                continue
            item = {
                "nomenclature_code": code,
                "name": _clean(row.get("name")),
            }
            item["model_tokens"] = _analog_model_tokens(item)
            identities[code] = item
            first_seen[code] = business_date
    return build_display_family_members(list(identities.values())), first_seen


def family_codes_for_focus(
    members: Mapping[str, DisplayFamilyMember], *, focus_code: str
) -> set[str]:
    focus = members.get(focus_code)
    if focus is None:
        raise ValueError(f"focus SKU not found in frozen decisions: {focus_code}")
    return {code for code, member in members.items() if member.family_id == focus.family_id}


def restrict_inputs(inputs: FrozenInputs, *, codes: set[str]) -> FrozenInputs:
    return FrozenInputs(
        fact_rows_by_date={
            business_date: [row for row in rows if _clean(row.get("nomenclature_code")) in codes]
            for business_date, rows in inputs.fact_rows_by_date.items()
        },
        fact_by_key={key: row for key, row in inputs.fact_by_key.items() if key[1] in codes},
        decision_rows_by_date={
            business_date: [row for row in rows if _clean(row.get("nomenclature_code")) in codes]
            for business_date, rows in inputs.decision_rows_by_date.items()
        },
        sales_by_code={code: rows for code, rows in inputs.sales_by_code.items() if code in codes},
        initial_pipeline_rows=[
            row
            for row in inputs.initial_pipeline_rows
            if _clean(row.get("nomenclature_code")) in codes
        ],
    )


def _recent_sales(
    sales_by_code: Mapping[str, Mapping[date, Decimal]],
    *,
    codes: Sequence[str],
    as_of: date,
    lookback_days: int,
) -> dict[str, Decimal]:
    start = as_of - timedelta(days=lookback_days)
    return {
        code: sum(
            (
                max(ZERO, Decimal(quantity))
                for business_date, quantity in sales_by_code.get(code, {}).items()
                if start <= business_date < as_of
            ),
            ZERO,
        )
        for code in codes
    }


def apply_family_forecast_allocation(
    *,
    decision_rows_by_date: Mapping[date, Sequence[Mapping[str, Any]]],
    sales_by_code: Mapping[str, Mapping[date, Decimal]],
    members: Mapping[str, DisplayFamilyMember],
    first_seen: Mapping[str, date],
    lookback_days: int,
    blend: Decimal,
    audit_codes: set[str] | None = None,
    replace_source_rows: bool = False,
) -> tuple[dict[date, list[dict[str, Any]]], list[dict[str, Any]]]:
    if lookback_days <= 0:
        raise ValueError("family lookback days must be positive")
    latest_rates: dict[str, Decimal] = {}
    transformed: dict[date, list[dict[str, Any]]] = {}
    audit: list[dict[str, Any]] = []
    family_codes: dict[str, list[str]] = defaultdict(list)
    for code, member in members.items():
        family_codes[member.family_id].append(code)

    for business_date in sorted(decision_rows_by_date):
        source_rows = decision_rows_by_date[business_date]
        for row in source_rows:
            code = _clean(row.get("nomenclature_code"))
            if code:
                latest_rates[code] = max(ZERO, _decimal(row.get("forecast_rate_sales")))

        active_codes = sorted(
            code
            for code in latest_rates
            if code in members and first_seen.get(code, business_date) <= business_date
        )
        recent = _recent_sales(
            sales_by_code,
            codes=active_codes,
            as_of=business_date,
            lookback_days=lookback_days,
        )
        allocations = allocate_display_family_rates(
            members,
            baseline_rates={code: latest_rates[code] for code in active_codes},
            recent_sales=recent,
            blend=blend,
        )
        output_rows: list[dict[str, Any]] = []
        for source in source_rows:
            row = dict(source)
            code = _clean(row.get("nomenclature_code"))
            allocation = allocations.get(code)
            if allocation is not None:
                row["forecast_rate_sales"] = str(allocation.allocated_rate)
                row["family_id"] = allocation.family_id
                row["family_segment_id"] = allocation.segment_id
                row["family_baseline_rate"] = str(allocation.family_baseline_rate)
                row["family_recent_sales_qty"] = str(allocation.family_recent_sales_qty)
                row["family_allocated_rate"] = str(allocation.allocated_rate)
                row["family_sku_share"] = str(allocation.sku_share)
                row["family_allocation_source"] = allocation.allocation_source
                if audit_codes is None or code in audit_codes:
                    audit.append(
                        {
                            "decision_date": business_date.isoformat(),
                            "nomenclature_code": code,
                            "name": _clean(row.get("name")),
                            "family_id": allocation.family_id,
                            "segment_id": allocation.segment_id,
                            "baseline_rate": str(allocation.baseline_rate),
                            "family_baseline_rate": str(allocation.family_baseline_rate),
                            "recent_sales_qty": str(allocation.recent_sales_qty),
                            "family_recent_sales_qty": str(allocation.family_recent_sales_qty),
                            "pure_family_rate": str(allocation.pure_family_rate),
                            "allocated_rate": str(allocation.allocated_rate),
                            "sku_share": str(allocation.sku_share),
                            "allocation_source": allocation.allocation_source,
                            "lookback_days": lookback_days,
                            "blend": str(blend),
                        }
                    )
            output_rows.append(row)
        transformed[business_date] = output_rows
        if replace_source_rows:
            if not isinstance(decision_rows_by_date, MutableMapping):
                raise TypeError("replace_source_rows requires a mutable decision mapping")
            decision_rows_by_date[business_date] = output_rows
    return transformed, audit


def _sku_metrics(result: Any, members: Mapping[str, DisplayFamilyMember]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for code, metric in sorted(result.model.items()):
        member = members.get(code)
        rows.append(
            {
                "nomenclature_code": code,
                "name": member.name if member else "",
                "family_id": member.family_id if member else "",
                "segment_id": member.segment_id if member else "",
                "served_sales_qty": str(metric.served_observed_qty),
                "lost_sales_qty": str(metric.lost_observed_qty),
                "gross_profit_rub": str(metric.gross_profit_rub),
                "order_qty": str(metric.order_qty),
                "order_value_rub": str(metric.order_value_rub),
                "ending_inventory_qty": str(metric.ending_inventory_qty),
                "ending_target_stock_qty": str(metric.ending_target_stock_qty),
                "ending_excess_stock_qty": str(
                    max(ZERO, metric.ending_inventory_qty - metric.ending_target_stock_qty)
                ),
            }
        )
    return rows


def _family_comparison(
    baseline_rows: Sequence[Mapping[str, Any]],
    candidate_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    metrics = (
        "served_sales_qty",
        "lost_sales_qty",
        "gross_profit_rub",
        "order_qty",
        "order_value_rub",
        "ending_inventory_qty",
        "ending_excess_stock_qty",
    )

    def aggregate(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Decimal]]:
        result: dict[str, dict[str, Decimal]] = defaultdict(
            lambda: {metric: ZERO for metric in metrics}
        )
        for row in rows:
            family_id = _clean(row.get("family_id"))
            for metric in metrics:
                result[family_id][metric] += _decimal(row.get(metric))
        return result

    baseline = aggregate(baseline_rows)
    candidate = aggregate(candidate_rows)
    output: list[dict[str, Any]] = []
    for family_id in sorted(set(baseline) | set(candidate)):
        row: dict[str, Any] = {"family_id": family_id}
        for metric in metrics:
            before = baseline[family_id][metric]
            after = candidate[family_id][metric]
            row[f"baseline_{metric}"] = str(before)
            row[f"candidate_{metric}"] = str(after)
            row[f"delta_{metric}"] = str(after - before)
        output.append(row)
    return output


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _load_candidate_parameters(path: Path) -> tuple[str, dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return str(payload["candidate_id"]), dict(payload["candidate_parameters"])


def _prepare(
    args: argparse.Namespace,
    *,
    date_to: date | None = None,
    scope_codes_override: set[str] | None = None,
) -> tuple[
    FrozenInputs,
    dict[str, DisplayFamilyMember],
    dict[str, date],
    set[str],
    Any,
    Any,
    Any,
    set[tuple[date, str]],
    dict[tuple[date, str], Decimal],
    Any,
]:
    policy_v2 = load_assortment_lifecycle_v2_policy(args.policy_json)
    effective_date_to = date_to or policy_v2.periods.training_to
    if effective_date_to > policy_v2.periods.holdout_to:
        raise ValueError("display family preparation exceeds the configured holdout")
    validate_preflight_directory(args.preflight_dir)
    inputs = load_frozen_inputs(args.preflight_dir, date_to=effective_date_to)
    members, first_seen = build_family_members_from_decisions(inputs.decision_rows_by_date)
    focus_codes = family_codes_for_focus(members, focus_code=args.focus_code)
    if scope_codes_override is not None:
        frozen_scope_codes = {
            str(code).strip() for code in scope_codes_override if str(code).strip()
        }
        if not frozen_scope_codes or not frozen_scope_codes <= focus_codes:
            raise ValueError("frozen family scope is absent from prepared inputs")
        scope_codes = frozen_scope_codes
        focus_codes = frozen_scope_codes
    else:
        scope_codes = focus_codes if args.scope == "targeted" else set(members)
    inputs = restrict_inputs(inputs, codes=scope_codes)
    members = {code: member for code, member in members.items() if code in scope_codes}
    first_seen = {code: value for code, value in first_seen.items() if code in scope_codes}
    candidate_id, parameters = _load_candidate_parameters(args.candidate_summary_json)
    lifecycle_hash, spike_keys, spike_rates = apply_v2_profile(
        lifecycle_csv=args.replay_dir / "v2-lifecycle-history.csv",
        fact_by_key=inputs.fact_by_key,
        profile=_profile(parameters),
        date_to=effective_date_to,
    )
    group_keys = _load_item_group_keys(args.replay_store_path, dataset_hash=args.dataset_hash)
    masks, bit_by_variant = build_representation_masks(
        inputs=inputs,
        group_keys_by_code=group_keys,
        group_sizes=policy_v2.backtest_grid.comparable_group_min_sizes,
        group_levels=policy_v2.backtest_grid.comparable_group_levels,
    )
    variant = (
        int(parameters["comparable_group_min_size"]),
        str(parameters["comparable_group_level"]),
    )
    representation = RepresentationMinimumLookup(
        eligibility_masks=masks,
        bit=bit_by_variant[variant],
        spike_keys=spike_keys,
    )
    auto_order_policy = load_auto_order_policy(args.auto_order_policy_json)
    scenario_config = load_scenario_config(args.scenario_config_json)
    base_scenario = next(
        row
        for row in _load_scenarios(args.preflight_dir / "scenario-decisions.csv")
        if row.scenario_id == args.control_scenario_id
    )
    scenario = _scenario_for_candidate(
        replace(base_scenario, legacy=False),
        candidate_id=candidate_id,
        parameters=parameters,
    )
    return (
        inputs,
        members,
        first_seen,
        focus_codes,
        policy_v2,
        auto_order_policy,
        scenario_config,
        spike_keys,
        spike_rates,
        representation,
        scenario,
        lifecycle_hash,
    )


def _run(args: argparse.Namespace) -> dict[str, Any]:
    (
        inputs,
        members,
        first_seen,
        focus_codes,
        policy_v2,
        auto_order_policy,
        scenario_config,
        spike_keys,
        spike_rates,
        representation,
        scenario,
        lifecycle_hash,
    ) = _prepare(args)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    preflight_sha256 = _sha256(args.preflight_dir / "run-manifest.json")
    family_mapping_hash = _run_fingerprint(
        {
            code: {
                "family_id": member.family_id,
                "segment_id": member.segment_id,
                "model_tokens": member.model_tokens,
                "first_seen": first_seen[code],
            }
            for code, member in sorted(members.items())
        }
    )
    run_identity = {
        "schema": "display_family_demand_backtest.v1",
        "scope": args.scope,
        "focus_code": args.focus_code,
        "scope_sku_count": len(members),
        "focus_family_sku_count": len(focus_codes),
        "lookback_days": args.lookback_days,
        "blend": str(args.blend),
        "dataset_hash": args.dataset_hash,
        "preflight_manifest_sha256": preflight_sha256,
        "lifecycle_hash": lifecycle_hash,
        "family_mapping_hash": family_mapping_hash,
        "scenario_id": scenario.scenario_id,
        "overall_metric_period_from": policy_v2.periods.training_from.isoformat(),
        "overall_metric_period_to": policy_v2.periods.training_to.isoformat(),
        "sku_metric_period_from": "2026-01-01",
        "sku_metric_period_to": policy_v2.periods.training_to.isoformat(),
    }
    fingerprint = _run_fingerprint(run_identity)
    manifest = {
        **run_identity,
        "run_fingerprint": fingerprint,
        "status": "prepared",
        "primary_analog_history_available": False,
        "primary_analog_history_blocker": (
            "frozen preflight has no dated primary_analog_effective_from; role is not used"
        ),
        "holdout_consumed": False,
        "production_authorized": False,
        "production_action": "none_read_only",
    }
    manifest_path = args.output_dir / "run-manifest.json"
    if args.resume and manifest_path.is_file():
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        if existing.get("run_fingerprint") != fingerprint:
            raise ValueError("resume manifest does not match the requested family backtest")
        manifest.update(existing)
        if existing.get("status") == "complete" and _artifacts_match(
            args.output_dir, existing.get("artifact_sha256") or {}
        ):
            return manifest
    _atomic_write_json(manifest_path, manifest)
    _write_csv(
        args.output_dir / "family-members.csv",
        [
            {
                **member.__dict__,
                "model_tokens": ";".join(member.model_tokens),
                "first_seen_date": first_seen[code].isoformat(),
                "is_focus_family": int(code in focus_codes),
            }
            for code, member in sorted(members.items())
        ],
    )

    if args.phase == "prepare":
        manifest["status"] = "prepared"
        manifest["artifact_sha256"] = {
            "family-members.csv": _sha256(args.output_dir / "family-members.csv")
        }
        _atomic_write_json(manifest_path, manifest)
        return manifest

    baseline_artifacts = {
        name: (manifest.get("artifact_sha256") or {}).get(name, "")
        for name in ("baseline-metrics.json", "baseline-sku-metrics.csv")
    }
    baseline_ready = args.resume and _artifacts_match(args.output_dir, baseline_artifacts)
    if baseline_ready:
        baseline_metrics = _read_metrics(args.output_dir / "baseline-metrics.json")
        baseline_sku = _read_csv(args.output_dir / "baseline-sku-metrics.csv")
    else:
        manifest["status"] = "running_baseline"
        _atomic_write_json(manifest_path, manifest)
        baseline = _simulate(
            inputs=inputs,
            scenario=replace(scenario, scenario_id=f"{scenario.scenario_id}-family-control"),
            policy=auto_order_policy,
            config=scenario_config,
            date_from=date(2026, 1, 1),
            date_to=policy_v2.periods.training_to,
            representation_minimums=representation,
            spike_keys=spike_keys,
            spike_rates=spike_rates,
            demand_sample_cache={},
        )
        baseline_metrics = _period_metrics(
            baseline,
            period_from=policy_v2.periods.training_from,
            period_to=policy_v2.periods.training_to,
        )
        baseline_sku = _sku_metrics(baseline, members)
        _atomic_write_json(args.output_dir / "baseline-metrics.json", baseline_metrics)
        _write_csv(args.output_dir / "baseline-sku-metrics.csv", baseline_sku)
        del baseline
        manifest.setdefault("artifact_sha256", {}).update(
            {
                "baseline-metrics.json": _sha256(args.output_dir / "baseline-metrics.json"),
                "baseline-sku-metrics.csv": _sha256(args.output_dir / "baseline-sku-metrics.csv"),
            }
        )
        manifest["status"] = "baseline_complete"
        _atomic_write_json(manifest_path, manifest)

    if args.phase == "baseline":
        return manifest

    manifest["status"] = "running_family_candidate"
    _atomic_write_json(manifest_path, manifest)
    transformed_decisions, audit = apply_family_forecast_allocation(
        decision_rows_by_date=inputs.decision_rows_by_date,
        sales_by_code=inputs.sales_by_code,
        members=members,
        first_seen=first_seen,
        lookback_days=args.lookback_days,
        blend=args.blend,
        audit_codes=focus_codes,
        replace_source_rows=True,
    )
    candidate_inputs = replace(inputs, decision_rows_by_date=transformed_decisions)
    candidate = _simulate(
        inputs=candidate_inputs,
        scenario=replace(scenario, scenario_id=f"{scenario.scenario_id}-family-candidate"),
        policy=auto_order_policy,
        config=scenario_config,
        date_from=date(2026, 1, 1),
        date_to=policy_v2.periods.training_to,
        representation_minimums=representation,
        spike_keys=spike_keys,
        spike_rates=spike_rates,
        demand_sample_cache={},
    )
    candidate_metrics = _period_metrics(
        candidate,
        period_from=policy_v2.periods.training_from,
        period_to=policy_v2.periods.training_to,
    )
    candidate_sku = _sku_metrics(candidate, members)
    comparison = {
        "baseline": baseline_metrics,
        "candidate": candidate_metrics,
        "deltas": _deltas(candidate_metrics, baseline_metrics),
        "passes_guardrails": all(
            (
                candidate_metrics["served_sales_qty"] >= baseline_metrics["served_sales_qty"],
                candidate_metrics["gross_profit_rub"] >= baseline_metrics["gross_profit_rub"],
                candidate_metrics["economic_effect_rub"] >= baseline_metrics["economic_effect_rub"],
                candidate_metrics["gmroi"] >= baseline_metrics["gmroi"],
                candidate_metrics["ending_excess_stock_qty"]
                <= baseline_metrics["ending_excess_stock_qty"],
            )
        ),
    }
    _atomic_write_json(args.output_dir / "candidate-metrics.json", candidate_metrics)
    _atomic_write_json(args.output_dir / "comparison.json", comparison)
    _write_csv(args.output_dir / "candidate-sku-metrics.csv", candidate_sku)
    _write_csv(args.output_dir / "focus-family-allocation-audit.csv", audit)
    _write_csv(
        args.output_dir / "family-comparison.csv",
        _family_comparison(baseline_sku, candidate_sku),
    )
    artifact_names = [
        "family-members.csv",
        "baseline-metrics.json",
        "candidate-metrics.json",
        "comparison.json",
        "baseline-sku-metrics.csv",
        "candidate-sku-metrics.csv",
        "family-comparison.csv",
        "focus-family-allocation-audit.csv",
    ]
    manifest.update(
        {
            "status": "complete",
            "passes_guardrails": comparison["passes_guardrails"],
            "artifacts": artifact_names,
            "artifact_sha256": {name: _sha256(args.output_dir / name) for name in artifact_names},
        }
    )
    _atomic_write_json(manifest_path, manifest)
    return manifest


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", choices=("targeted", "all"), default="targeted")
    parser.add_argument(
        "--phase", choices=("prepare", "baseline", "candidate", "all"), default="all"
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--focus-code", default=DEFAULT_FOCUS_CODE)
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    parser.add_argument("--blend", type=Decimal, default=DEFAULT_BLEND)
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
    parser.add_argument("--candidate-summary-json", type=Path, default=DEFAULT_CANDIDATE_SUMMARY)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    manifest = _run(args)
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
