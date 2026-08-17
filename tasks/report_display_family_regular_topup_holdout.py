"""Consume the July holdout once for the frozen regular-topup-c100 candidate."""

from __future__ import annotations

import csv
import json
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.services.display_family_demand import (
    allocate_display_family_order_pool,
    build_display_family_regular_topup,
    build_regular_topup_delivery_overrides,
    freeze_display_family_order_trajectory,
)
from tasks.report_display_auto_order_frozen_backtest import _decimal
from tasks.report_display_family_demand_backtest import (
    _atomic_write_json,
    _prepare,
    _run_fingerprint,
    _sha256,
    _write_csv,
)
from tasks.report_display_family_profit_recovery_backtest import (
    _actual_period_metrics,
    _run_candidate,
    _scenario_row,
)
from tasks.report_display_family_regular_topup_backtest import (
    DEFAULT_OUTPUT_DIR as TRAINING_RUNNER_OUTPUT_DIR,
)
from tasks.report_display_family_regular_topup_backtest import (
    _assert_monotonic_served_sales,
    _audit_dict,
    _focus_loss_metrics,
)
from tasks.report_display_family_regular_topup_backtest import (
    _parse_args as _parse_training_args,
)
from tasks.run_assortment_lifecycle_v2_economic_backtest import _period_metrics, _simulate

ZERO = Decimal("0")
FROZEN_COVERAGE = Decimal("1.00")
FROZEN_SCENARIO_ID = "regular-topup-c100"
TRAINING_OUTPUT_DIR = Path(
    "reports/assortment_lifecycle/backtest-2026-01-01_2026-07-31/"
    "display-family-regular-topup-backtest-2026-08-15/"
    "targeted-iphone14pm-weekly-cadence"
)
DEFAULT_OUTPUT_DIR = Path(
    "reports/assortment_lifecycle/backtest-2026-01-01_2026-07-31/"
    "display-family-regular-topup-holdout-2026-08-15/"
    "targeted-iphone14pm-c100-v2"
)


def _load_training_freeze(
    training_dir: Path, *, expected_dataset_hash: str
) -> tuple[dict[str, Any], Decimal, int]:
    manifest_path = training_dir / "run-manifest.json"
    summary_path = training_dir / "scenario-summary.csv"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") != "complete":
        raise ValueError("frozen training run is not complete")
    if manifest.get("schema") != "display_family_regular_topup_backtest.v2":
        raise ValueError("frozen training run predates the corrected P75 contour")
    if manifest.get("topup_actual_arrival_quantile") != "p75":
        raise ValueError("frozen training run did not simulate P75 top-up arrivals")
    if manifest.get("baseline_order_trajectory_protected") is not True:
        raise ValueError("frozen training run did not protect the baseline order trajectory")
    if manifest.get("holdout_consumed") is not False:
        raise ValueError("training source must not have consumed holdout")
    if manifest.get("best_passing_scenario") != FROZEN_SCENARIO_ID:
        raise ValueError("frozen training winner is not regular-topup-c100")
    if manifest.get("dataset_hash") != expected_dataset_hash:
        raise ValueError("holdout dataset does not match frozen training dataset")
    if manifest.get("coverage_fractions") != ["0.25", "0.50", "0.75", "1.00"]:
        raise ValueError("frozen training grid identity changed")
    cadence_days = int(manifest.get("order_cadence_days") or 0)
    if cadence_days != 7:
        raise ValueError("frozen training cadence must be seven days")

    with summary_path.open(encoding="utf-8-sig", newline="") as source:
        rows = {row["scenario_id"]: row for row in csv.DictReader(source)}
    current = rows.get("current-family-order-pool")
    candidate = rows.get(FROZEN_SCENARIO_ID)
    if current is None or candidate is None:
        raise ValueError("frozen training summary is incomplete")
    if candidate.get("coverage_fraction") != "1.00":
        raise ValueError("frozen training coverage changed")
    if candidate.get("passes_current_guardrails") != "1":
        raise ValueError("frozen training candidate did not pass guardrails")
    return manifest, _decimal(current.get("gmroi")), cadence_days


def _assert_holdout_not_consumed(manifest_path: Path) -> None:
    if not manifest_path.exists():
        return
    existing = json.loads(manifest_path.read_text(encoding="utf-8"))
    if existing.get("holdout_consumed") is True:
        raise RuntimeError("July holdout has already been consumed for this frozen candidate")


def _run(args: Any) -> dict[str, Any]:
    args.scope = "targeted"
    training_prepared = _prepare(args)
    training_focus_codes = set(training_prepared[3])
    training_policy_v2 = training_prepared[4]
    holdout_from = training_policy_v2.periods.holdout_from
    holdout_to = training_policy_v2.periods.holdout_to
    if holdout_from != date(2026, 7, 1) or holdout_to != date(2026, 7, 31):
        raise ValueError("unexpected holdout window")
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
    ) = _prepare(
        args,
        date_to=holdout_to,
        scope_codes_override=training_focus_codes,
    )
    if set(members) != focus_codes or args.focus_code not in focus_codes:
        raise ValueError("regular top-up holdout runner escaped the frozen focus family")
    if focus_codes != training_focus_codes:
        raise ValueError("regular top-up holdout family differs from frozen training scope")

    holdout_fact_rows = sum(
        len(rows)
        for business_date, rows in inputs.fact_rows_by_date.items()
        if holdout_from <= business_date <= holdout_to
    )
    holdout_decision_rows = sum(
        len(rows)
        for business_date, rows in inputs.decision_rows_by_date.items()
        if holdout_from <= business_date <= holdout_to
    )
    holdout_business_dates = {
        business_date
        for business_date in inputs.fact_rows_by_date
        if holdout_from <= business_date <= holdout_to
    }
    if holdout_fact_rows <= 0 or len(holdout_business_dates) <= 0:
        raise ValueError("July holdout facts are absent from prepared frozen inputs")

    training_manifest, frozen_gmroi_hurdle, cadence_days = _load_training_freeze(
        TRAINING_OUTPUT_DIR,
        expected_dataset_hash=args.dataset_hash,
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = args.output_dir / "run-manifest.json"
    _assert_holdout_not_consumed(manifest_path)
    identity = {
        "schema": "display_family_regular_topup_holdout.v1",
        "focus_code": args.focus_code,
        "scope_sku_count": len(members),
        "candidate_scenario_id": FROZEN_SCENARIO_ID,
        "coverage_fraction": str(FROZEN_COVERAGE),
        "delivery_mode": "ordinary_p75_only",
        "order_cadence_days": cadence_days,
        "frozen_gmroi_hurdle": str(frozen_gmroi_hurdle),
        "holdout_from": holdout_from.isoformat(),
        "holdout_to": holdout_to.isoformat(),
        "holdout_fact_row_count": holdout_fact_rows,
        "holdout_decision_row_count": holdout_decision_rows,
        "holdout_business_date_count": len(holdout_business_dates),
        "dataset_hash": args.dataset_hash,
        "lifecycle_hash": lifecycle_hash,
        "training_run_fingerprint": training_manifest["run_fingerprint"],
        "training_manifest_sha256": _sha256(TRAINING_OUTPUT_DIR / "run-manifest.json"),
        "one_shot_holdout": True,
        "holdout_consumed": False,
        "production_authorized": False,
        "production_action": "none_read_only",
    }
    identity["run_fingerprint"] = _run_fingerprint(identity)
    manifest = {**identity, "status": "running_control"}
    _atomic_write_json(manifest_path, manifest)

    control = _simulate(
        inputs=inputs,
        scenario=replace(scenario, scenario_id=f"{scenario.scenario_id}-holdout-control"),
        policy=auto_order_policy,
        config=scenario_config,
        date_from=date(2026, 1, 1),
        date_to=holdout_to,
        representation_minimums=representation,
        spike_keys=spike_keys,
        spike_rates=spike_rates,
        demand_sample_cache={},
        keep_decision_detail=True,
    )
    actual_metrics = _actual_period_metrics(
        control,
        period_from=holdout_from,
        period_to=holdout_to,
    )
    current_overrides, _current_allocation = allocate_display_family_order_pool(
        control.decision_rows,
        members=members,
        sales_by_code=inputs.sales_by_code,
        short_lookback_days=30,
        long_lookback_days=90,
        max_share_step=Decimal("0.10"),
        capital_cap_fraction=ZERO,
        one_open_family_lot=True,
    )
    current_result = _run_candidate(
        inputs=inputs,
        scenario=scenario,
        scenario_id=f"{scenario.scenario_id}-holdout-current",
        auto_order_policy=auto_order_policy,
        scenario_config=scenario_config,
        period_to=holdout_to,
        representation=representation,
        spike_keys=spike_keys,
        spike_rates=spike_rates,
        overrides=current_overrides,
        keep_loss_detail=True,
    )
    current_metrics = _period_metrics(
        current_result,
        period_from=holdout_from,
        period_to=holdout_to,
    )
    current_loss_qty, current_loss_profit, current_loss_rows = _focus_loss_metrics(
        current_result.loss_rows,
        focus_code=args.focus_code,
        period_from=holdout_from,
        period_to=holdout_to,
    )
    frozen_current_overrides = freeze_display_family_order_trajectory(current_result.decision_rows)

    overrides, audit = build_display_family_regular_topup(
        current_result.decision_rows,
        base_overrides=frozen_current_overrides,
        focus_codes={args.focus_code},
        annual_carrying_rate=scenario.cost.total_annual_rate,
        shortage_coverage_fraction=FROZEN_COVERAGE,
        gmroi_hurdle=frozen_gmroi_hurdle,
        latest_evaluable_arrival_date=holdout_to,
        minimum_days_between_topups=cadence_days,
    )
    topup_qty_overrides, topup_arrival_date_overrides = build_regular_topup_delivery_overrides(
        audit
    )
    manifest["status"] = "running_frozen_candidate"
    _atomic_write_json(manifest_path, manifest)
    candidate_result = _run_candidate(
        inputs=inputs,
        scenario=scenario,
        scenario_id=f"{scenario.scenario_id}-holdout-{FROZEN_SCENARIO_ID}",
        auto_order_policy=auto_order_policy,
        scenario_config=scenario_config,
        period_to=holdout_to,
        representation=representation,
        spike_keys=spike_keys,
        spike_rates=spike_rates,
        overrides=overrides,
        topup_qty_overrides=topup_qty_overrides,
        topup_arrival_date_overrides=topup_arrival_date_overrides,
        keep_loss_detail=True,
    )
    _assert_monotonic_served_sales(current=current_result, candidate=candidate_result)
    candidate_metrics = _period_metrics(
        candidate_result,
        period_from=holdout_from,
        period_to=holdout_to,
    )
    candidate_loss_qty, candidate_loss_profit, candidate_loss_rows = _focus_loss_metrics(
        candidate_result.loss_rows,
        focus_code=args.focus_code,
        period_from=holdout_from,
        period_to=holdout_to,
    )
    added_qty = sum((row.added_order_qty for row in audit), ZERO)
    added_value = sum(
        (row.added_order_qty * row.inventory_cost_per_unit_rub for row in audit),
        ZERO,
    )
    scenario_rows = [
        {
            **_scenario_row(
                scenario_id="current-family-order-pool",
                metrics=current_metrics,
                current=current_metrics,
                actual=actual_metrics,
            ),
            "coverage_fraction": "0",
            "focus_added_order_qty": "0",
            "focus_added_order_value_rub": "0",
            "focus_lost_sales_qty": str(current_loss_qty),
            "focus_lost_gross_profit_rub": str(current_loss_profit),
            "focus_recovered_sales_qty": "0",
            "focus_recovered_gross_profit_rub": "0",
        },
        {
            **_scenario_row(
                scenario_id=FROZEN_SCENARIO_ID,
                metrics=candidate_metrics,
                current=current_metrics,
                actual=actual_metrics,
            ),
            "coverage_fraction": str(FROZEN_COVERAGE),
            "focus_added_order_qty": str(added_qty),
            "focus_added_order_value_rub": str(added_value),
            "focus_lost_sales_qty": str(candidate_loss_qty),
            "focus_lost_gross_profit_rub": str(candidate_loss_profit),
            "focus_recovered_sales_qty": str(current_loss_qty - candidate_loss_qty),
            "focus_recovered_gross_profit_rub": str(current_loss_profit - candidate_loss_profit),
        },
    ]
    candidate_row = scenario_rows[1]
    holdout_passes = bool(candidate_row["passes_current_guardrails"] == 1)
    promotion_candidate = bool(
        holdout_passes
        and _decimal(candidate_row["gross_profit_delta_vs_current_rub"]) > ZERO
        and _decimal(candidate_row["economic_effect_delta_vs_current_rub"]) > ZERO
    )
    loss_rows = [{"scenario_id": "current-family-order-pool", **row} for row in current_loss_rows]
    loss_rows.extend({"scenario_id": FROZEN_SCENARIO_ID, **row} for row in candidate_loss_rows)
    audit_rows = [{"scenario_id": FROZEN_SCENARIO_ID, **_audit_dict(row)} for row in audit]
    _write_csv(args.output_dir / "scenario-summary.csv", scenario_rows)
    _write_csv(args.output_dir / "focus-loss-events.csv", loss_rows)
    _write_csv(args.output_dir / "regular-topup-audit.csv", audit_rows)
    _atomic_write_json(
        args.output_dir / "training-freeze.json",
        {
            "scenario_id": FROZEN_SCENARIO_ID,
            "coverage_fraction": str(FROZEN_COVERAGE),
            "order_cadence_days": cadence_days,
            "gmroi_hurdle": str(frozen_gmroi_hurdle),
            "training_run_fingerprint": training_manifest["run_fingerprint"],
            "training_manifest_sha256": identity["training_manifest_sha256"],
        },
    )
    artifacts = [
        "scenario-summary.csv",
        "focus-loss-events.csv",
        "regular-topup-audit.csv",
        "training-freeze.json",
    ]
    manifest.update(
        {
            "status": "complete",
            "holdout_consumed": True,
            "holdout_passes_guardrails": holdout_passes,
            "promotion_candidate": promotion_candidate,
            "holdout_result": "pass" if promotion_candidate else "fail",
            "artifacts": artifacts,
            "artifact_sha256": {name: _sha256(args.output_dir / name) for name in artifacts},
        }
    )
    _atomic_write_json(manifest_path, manifest)
    return manifest


def _parse_args(argv: list[str] | None = None) -> Any:
    args = _parse_training_args(argv)
    if args.output_dir == TRAINING_RUNNER_OUTPUT_DIR:
        args.output_dir = DEFAULT_OUTPUT_DIR
    return args


def main(argv: list[str] | None = None) -> int:
    manifest = _run(_parse_args(argv))
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
