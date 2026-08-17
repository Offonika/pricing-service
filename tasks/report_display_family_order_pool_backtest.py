"""Run the targeted iPhone 14 Pro Max family order-pool challenger.

The runner is read-only.  It uses the ordinary frozen-simulator order need
after stock/reserve/reliable pipeline, redistributes only that need inside a
confirmed quality/construction segment, and never opens the holdout period.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from app.services.assortment_lifecycle_v2_policy import (
    DEFAULT_ASSORTMENT_LIFECYCLE_V2_POLICY_PATH,
)
from app.services.display_family_demand import allocate_display_family_order_pool
from tasks.report_display_auto_order_frozen_backtest import _clean, _decimal
from tasks.report_display_family_demand_backtest import (
    DEFAULT_CANDIDATE_SUMMARY,
    DEFAULT_FOCUS_CODE,
    _atomic_write_json,
    _family_comparison,
    _prepare,
    _run_fingerprint,
    _sha256,
    _sku_metrics,
    _write_csv,
)
from tasks.run_assortment_lifecycle_v2_economic_backtest import (
    DEFAULT_CONTROL_SCENARIO_ID,
    DEFAULT_DATASET_HASH,
    DEFAULT_PREFLIGHT_DIR,
    DEFAULT_REPLAY_DIR,
    DEFAULT_REPLAY_STORE,
    _deltas,
    _period_metrics,
    _simulate,
)

ZERO = Decimal("0")
DEFAULT_OUTPUT_DIR = Path(
    "reports/assortment_lifecycle/backtest-2026-01-01_2026-07-31/"
    "display-family-order-pool-backtest-2026-08-15/targeted-iphone14pm"
)


def _audit_rows(
    allocations: Sequence[Any],
    baseline_decisions: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    baseline_by_key = {
        (str(row.get("decision_date")), _clean(row.get("nomenclature_code"))): row
        for row in baseline_decisions
    }
    output: list[dict[str, Any]] = []
    for allocation in allocations:
        row = asdict(allocation)
        source = baseline_by_key.get(
            (allocation.decision_date.isoformat(), allocation.nomenclature_code), {}
        )
        row.update(
            {
                "decision_date": allocation.decision_date.isoformat(),
                "model_stock_qty": source.get("model_stock_qty", ""),
                "reserve_qty": source.get("reserve_qty", ""),
                "model_pipeline_qty": source.get("model_pipeline_qty", ""),
                "effective_model_pipeline_qty": source.get("effective_model_pipeline_qty", ""),
                "inventory_position_qty": source.get("inventory_position_qty", ""),
                "target_stock_qty": source.get("target_stock_qty", ""),
                "recommended_order_qty_raw": source.get("recommended_order_qty_raw", ""),
                "expected_arrival_date": source.get("expected_arrival_date", ""),
            }
        )
        output.append(row)
    return output


def _sku_report(
    *,
    baseline_rows: Sequence[Mapping[str, Any]],
    candidate_rows: Sequence[Mapping[str, Any]],
    audit_rows: Sequence[Mapping[str, Any]],
    members: Mapping[str, Any],
    focus_code: str,
) -> list[dict[str, Any]]:
    baseline = {_clean(row.get("nomenclature_code")): row for row in baseline_rows}
    candidate = {_clean(row.get("nomenclature_code")): row for row in candidate_rows}
    allocation_totals: dict[str, dict[str, Any]] = {}
    for row in audit_rows:
        code = _clean(row.get("nomenclature_code"))
        totals = allocation_totals.setdefault(
            code,
            {
                "ordinary_pool_baseline_qty": ZERO,
                "ordinary_pool_allocated_qty": ZERO,
                "changed_decision_count": 0,
                "blocked_decision_count": 0,
            },
        )
        before = _decimal(row.get("baseline_order_qty"))
        after = _decimal(row.get("allocated_order_qty"))
        totals["ordinary_pool_baseline_qty"] += before
        totals["ordinary_pool_allocated_qty"] += after
        totals["changed_decision_count"] += int(before != after)
        totals["blocked_decision_count"] += int(bool(_clean(row.get("blocker"))))

    output: list[dict[str, Any]] = []
    metric_fields = (
        "served_sales_qty",
        "lost_sales_qty",
        "gross_profit_rub",
        "order_qty",
        "order_value_rub",
        "ending_inventory_qty",
        "ending_excess_stock_qty",
    )
    for code in sorted(set(baseline) | set(candidate)):
        before = baseline.get(code, {})
        after = candidate.get(code, {})
        totals = allocation_totals.get(code, {})
        member = members[code]
        row: dict[str, Any] = {
            "nomenclature_code": code,
            "is_focus_sku": int(code == focus_code),
            "name": member.name,
            "family_id": member.family_id,
            "segment_id": member.segment_id,
            "ordinary_pool_baseline_qty": str(totals.get("ordinary_pool_baseline_qty", ZERO)),
            "ordinary_pool_allocated_qty": str(totals.get("ordinary_pool_allocated_qty", ZERO)),
            "ordinary_pool_net_shift_qty": str(
                totals.get("ordinary_pool_allocated_qty", ZERO)
                - totals.get("ordinary_pool_baseline_qty", ZERO)
            ),
            "changed_decision_count": totals.get("changed_decision_count", 0),
            "blocked_decision_count": totals.get("blocked_decision_count", 0),
        }
        for field in metric_fields:
            baseline_value = _decimal(before.get(field))
            candidate_value = _decimal(after.get(field))
            row[f"baseline_{field}"] = str(baseline_value)
            row[f"candidate_{field}"] = str(candidate_value)
            row[f"delta_{field}"] = str(candidate_value - baseline_value)
        output.append(row)
    return output


def _run(args: argparse.Namespace) -> dict[str, Any]:
    args.scope = "targeted"
    (
        inputs,
        members,
        _first_seen,
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
    if set(members) != focus_codes:
        raise ValueError("targeted runner escaped the frozen focus family")
    period_to = policy_v2.periods.training_to
    if period_to >= date(2026, 7, 1):
        raise ValueError("targeted family challenger must not consume July holdout")

    identity = {
        "schema": "display_family_order_pool_backtest.v1",
        "focus_code": args.focus_code,
        "scope_sku_count": len(members),
        "short_lookback_days": args.short_lookback_days,
        "long_lookback_days": args.long_lookback_days,
        "max_share_step": str(args.max_share_step),
        "capital_cap_fraction": str(args.capital_cap_fraction),
        "one_open_family_lot": True,
        "dataset_hash": args.dataset_hash,
        "lifecycle_hash": lifecycle_hash,
        "scenario_id": scenario.scenario_id,
        "simulation_from": "2026-01-01",
        "training_to": period_to.isoformat(),
        "holdout_consumed": False,
        "production_authorized": False,
        "production_action": "none_read_only",
    }
    identity["run_fingerprint"] = _run_fingerprint(identity)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = args.output_dir / "run-manifest.json"
    manifest = {**identity, "status": "running_baseline"}
    _atomic_write_json(manifest_path, manifest)

    baseline = _simulate(
        inputs=inputs,
        scenario=replace(scenario, scenario_id=f"{scenario.scenario_id}-family-order-control"),
        policy=auto_order_policy,
        config=scenario_config,
        date_from=date(2026, 1, 1),
        date_to=period_to,
        representation_minimums=representation,
        spike_keys=spike_keys,
        spike_rates=spike_rates,
        demand_sample_cache={},
        keep_decision_detail=True,
    )
    baseline_metrics = _period_metrics(
        baseline,
        period_from=policy_v2.periods.training_from,
        period_to=period_to,
    )
    baseline_sku = _sku_metrics(baseline, members)
    order_overrides, allocations = allocate_display_family_order_pool(
        baseline.decision_rows,
        members=members,
        sales_by_code=inputs.sales_by_code,
        short_lookback_days=args.short_lookback_days,
        long_lookback_days=args.long_lookback_days,
        max_share_step=args.max_share_step,
        capital_cap_fraction=args.capital_cap_fraction,
        one_open_family_lot=True,
    )
    audit = _audit_rows(allocations, baseline.decision_rows)
    manifest["status"] = "running_candidate"
    _atomic_write_json(manifest_path, manifest)

    candidate = _simulate(
        inputs=inputs,
        scenario=replace(scenario, scenario_id=f"{scenario.scenario_id}-family-order-candidate"),
        policy=auto_order_policy,
        config=scenario_config,
        date_from=date(2026, 1, 1),
        date_to=period_to,
        representation_minimums=representation,
        spike_keys=spike_keys,
        spike_rates=spike_rates,
        demand_sample_cache={},
        keep_decision_detail=True,
        ordinary_order_overrides=order_overrides,
    )
    candidate_metrics = _period_metrics(
        candidate,
        period_from=policy_v2.periods.training_from,
        period_to=period_to,
    )
    candidate_sku = _sku_metrics(candidate, members)
    sku_report = _sku_report(
        baseline_rows=baseline_sku,
        candidate_rows=candidate_sku,
        audit_rows=audit,
        members=members,
        focus_code=args.focus_code,
    )
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

    _atomic_write_json(args.output_dir / "baseline-metrics.json", baseline_metrics)
    _atomic_write_json(args.output_dir / "candidate-metrics.json", candidate_metrics)
    _atomic_write_json(args.output_dir / "comparison.json", comparison)
    _write_csv(args.output_dir / "baseline-sku-metrics.csv", baseline_sku)
    _write_csv(args.output_dir / "candidate-sku-metrics.csv", candidate_sku)
    _write_csv(args.output_dir / "order-pool-allocation-audit.csv", audit)
    _write_csv(args.output_dir / "sku-address-report.csv", sku_report)
    _write_csv(
        args.output_dir / "family-comparison.csv",
        _family_comparison(baseline_sku, candidate_sku),
    )
    artifacts = [
        "baseline-metrics.json",
        "candidate-metrics.json",
        "comparison.json",
        "baseline-sku-metrics.csv",
        "candidate-sku-metrics.csv",
        "order-pool-allocation-audit.csv",
        "sku-address-report.csv",
        "family-comparison.csv",
    ]
    manifest.update(
        {
            "status": "complete",
            "passes_guardrails": comparison["passes_guardrails"],
            "changed_order_decisions": sum(
                int(row.baseline_order_qty != row.allocated_order_qty) for row in allocations
            ),
            "artifacts": artifacts,
            "artifact_sha256": {name: _sha256(args.output_dir / name) for name in artifacts},
        }
    )
    _atomic_write_json(manifest_path, manifest)
    return manifest


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--focus-code", default=DEFAULT_FOCUS_CODE)
    parser.add_argument("--short-lookback-days", type=int, default=30)
    parser.add_argument("--long-lookback-days", type=int, default=90)
    parser.add_argument("--max-share-step", type=Decimal, default=Decimal("0.20"))
    parser.add_argument("--capital-cap-fraction", type=Decimal, default=ZERO)
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
    # Compatibility attributes consumed by the existing frozen-family preparer.
    parser.set_defaults(lookback_days=14, blend=Decimal("0.5"), scope="targeted")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    manifest = _run(_parse_args(argv))
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
