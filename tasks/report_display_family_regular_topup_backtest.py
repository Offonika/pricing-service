"""Test early ordinary-channel top-ups for one frozen display-family SKU."""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from app.services.display_family_demand import (
    DisplayFamilyRegularTopUp,
    allocate_display_family_order_pool,
    build_display_family_regular_topup,
    build_regular_topup_delivery_overrides,
    freeze_display_family_order_trajectory,
)
from tasks.report_display_auto_order_frozen_backtest import _clean, _decimal
from tasks.report_display_family_demand_backtest import (
    _atomic_write_json,
    _prepare,
    _run_fingerprint,
    _sha256,
    _write_csv,
)
from tasks.report_display_family_profit_recovery_backtest import (
    DEFAULT_OUTPUT_DIR as PROFIT_RECOVERY_OUTPUT_DIR,
)
from tasks.report_display_family_profit_recovery_backtest import (
    _actual_period_metrics,
    _run_candidate,
    _scenario_row,
)
from tasks.report_display_family_profit_recovery_backtest import (
    _parse_args as _parse_profit_recovery_args,
)
from tasks.run_assortment_lifecycle_v2_economic_backtest import _period_metrics, _simulate

ZERO = Decimal("0")
COVERAGE_FRACTIONS = (
    Decimal("0.25"),
    Decimal("0.50"),
    Decimal("0.75"),
    Decimal("1.00"),
)
DEFAULT_OUTPUT_DIR = Path(
    "reports/assortment_lifecycle/backtest-2026-01-01_2026-07-31/"
    "display-family-regular-topup-backtest-2026-08-15/"
    "targeted-iphone14pm-p75-baseline-protected"
)


def _focus_loss_metrics(
    loss_rows: Sequence[Mapping[str, Any]],
    *,
    focus_code: str,
    period_from: date,
    period_to: date,
) -> tuple[Decimal, Decimal, list[dict[str, Any]]]:
    lost_qty = ZERO
    lost_gross_profit = ZERO
    details: list[dict[str, Any]] = []
    for row in loss_rows:
        business_date = date.fromisoformat(_clean(row.get("business_date")))
        if not period_from <= business_date <= period_to:
            continue
        if _clean(row.get("nomenclature_code")) != focus_code:
            continue
        row_lost_qty = _decimal(row.get("lost_observed_qty"))
        row_margin = _decimal(row.get("gross_margin_per_unit_rub"))
        row_lost_profit = row_lost_qty * row_margin
        lost_qty += row_lost_qty
        lost_gross_profit += row_lost_profit
        details.append(
            {
                "business_date": business_date.isoformat(),
                "nomenclature_code": focus_code,
                "observed_demand_qty": str(_decimal(row.get("observed_demand_qty"))),
                "served_observed_qty": str(_decimal(row.get("served_observed_qty"))),
                "lost_observed_qty": str(row_lost_qty),
                "lost_gross_profit_rub": str(row_lost_profit),
                "model_stock_before_demand_qty": str(
                    _decimal(row.get("model_stock_before_demand_qty"))
                ),
                "model_pipeline_qty": str(_decimal(row.get("model_pipeline_qty"))),
                "next_pipeline_arrival_date": _clean(row.get("next_pipeline_arrival_date")),
                "days_to_next_pipeline_arrival": _clean(row.get("days_to_next_pipeline_arrival")),
            }
        )
    return lost_qty, lost_gross_profit, details


def _audit_dict(row: DisplayFamilyRegularTopUp) -> dict[str, Any]:
    payload = asdict(row)
    for key, value in payload.items():
        if isinstance(value, date):
            payload[key] = value.isoformat()
    return payload


def _assert_monotonic_served_sales(*, current: Any, candidate: Any) -> None:
    for code, current_metric in current.model.items():
        candidate_metric = candidate.model[code]
        if candidate_metric.served_observed_qty < current_metric.served_observed_qty:
            raise RuntimeError(
                "regular top-up reduced served sales for "
                f"{code}: {current_metric.served_observed_qty} -> "
                f"{candidate_metric.served_observed_qty}"
            )


def _run(args: Any) -> dict[str, Any]:
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
    if set(members) != focus_codes or args.focus_code not in focus_codes:
        raise ValueError("regular top-up runner escaped the frozen focus family")
    period_to = policy_v2.periods.training_to
    if period_to >= date(2026, 7, 1):
        raise ValueError("regular top-up runner must not consume July holdout")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = args.output_dir / "run-manifest.json"
    identity = {
        "schema": "display_family_regular_topup_backtest.v2",
        "focus_code": args.focus_code,
        "scope_sku_count": len(members),
        "training_to": period_to.isoformat(),
        "coverage_fractions": [str(value) for value in COVERAGE_FRACTIONS],
        "delivery_mode": "ordinary_p75_only",
        "topup_actual_arrival_quantile": "p75",
        "baseline_order_trajectory_protected": True,
        "monotonic_served_sales_required": True,
        "order_cadence_days": auto_order_policy.order_cadence_days,
        "dataset_hash": args.dataset_hash,
        "lifecycle_hash": lifecycle_hash,
        "holdout_consumed": False,
        "production_authorized": False,
        "production_action": "none_read_only",
    }
    identity["run_fingerprint"] = _run_fingerprint(identity)
    manifest = {**identity, "status": "running_control"}
    _atomic_write_json(manifest_path, manifest)

    control = _simulate(
        inputs=inputs,
        scenario=replace(scenario, scenario_id=f"{scenario.scenario_id}-regular-control"),
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
    actual_metrics = _actual_period_metrics(
        control,
        period_from=policy_v2.periods.training_from,
        period_to=period_to,
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
        scenario_id=f"{scenario.scenario_id}-regular-current",
        auto_order_policy=auto_order_policy,
        scenario_config=scenario_config,
        period_to=period_to,
        representation=representation,
        spike_keys=spike_keys,
        spike_rates=spike_rates,
        overrides=current_overrides,
        keep_loss_detail=True,
    )
    current_metrics = _period_metrics(
        current_result,
        period_from=policy_v2.periods.training_from,
        period_to=period_to,
    )
    current_loss_qty, current_loss_profit, current_loss_rows = _focus_loss_metrics(
        current_result.loss_rows,
        focus_code=args.focus_code,
        period_from=policy_v2.periods.training_from,
        period_to=period_to,
    )
    frozen_current_overrides = freeze_display_family_order_trajectory(current_result.decision_rows)
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
        }
    ]
    detailed_loss_rows = [
        {"scenario_id": "current-family-order-pool", **row} for row in current_loss_rows
    ]
    audit_rows: list[dict[str, Any]] = []
    for coverage in COVERAGE_FRACTIONS:
        scenario_id = f"regular-topup-c{int(coverage * Decimal('100'))}"
        manifest["status"] = f"running_{scenario_id}"
        _atomic_write_json(manifest_path, manifest)
        overrides, audit = build_display_family_regular_topup(
            current_result.decision_rows,
            base_overrides=frozen_current_overrides,
            focus_codes={args.focus_code},
            annual_carrying_rate=scenario.cost.total_annual_rate,
            shortage_coverage_fraction=coverage,
            gmroi_hurdle=current_metrics["gmroi"],
            latest_evaluable_arrival_date=period_to,
            minimum_days_between_topups=auto_order_policy.order_cadence_days,
        )
        topup_qty_overrides, topup_arrival_date_overrides = build_regular_topup_delivery_overrides(
            audit
        )
        result = _run_candidate(
            inputs=inputs,
            scenario=scenario,
            scenario_id=f"{scenario.scenario_id}-{scenario_id}",
            auto_order_policy=auto_order_policy,
            scenario_config=scenario_config,
            period_to=period_to,
            representation=representation,
            spike_keys=spike_keys,
            spike_rates=spike_rates,
            overrides=overrides,
            topup_qty_overrides=topup_qty_overrides,
            topup_arrival_date_overrides=topup_arrival_date_overrides,
            keep_loss_detail=True,
        )
        _assert_monotonic_served_sales(current=current_result, candidate=result)
        metrics = _period_metrics(
            result,
            period_from=policy_v2.periods.training_from,
            period_to=period_to,
        )
        lost_qty, lost_profit, loss_details = _focus_loss_metrics(
            result.loss_rows,
            focus_code=args.focus_code,
            period_from=policy_v2.periods.training_from,
            period_to=period_to,
        )
        added_qty = sum((row.added_order_qty for row in audit), ZERO)
        added_value = sum(
            (row.added_order_qty * row.inventory_cost_per_unit_rub for row in audit),
            ZERO,
        )
        scenario_rows.append(
            {
                **_scenario_row(
                    scenario_id=scenario_id,
                    metrics=metrics,
                    current=current_metrics,
                    actual=actual_metrics,
                ),
                "coverage_fraction": str(coverage),
                "focus_added_order_qty": str(added_qty),
                "focus_added_order_value_rub": str(added_value),
                "focus_lost_sales_qty": str(lost_qty),
                "focus_lost_gross_profit_rub": str(lost_profit),
                "focus_recovered_sales_qty": str(current_loss_qty - lost_qty),
                "focus_recovered_gross_profit_rub": str(current_loss_profit - lost_profit),
            }
        )
        detailed_loss_rows.extend({"scenario_id": scenario_id, **row} for row in loss_details)
        audit_rows.extend({"scenario_id": scenario_id, **_audit_dict(row)} for row in audit)

    passing = [
        row
        for row in scenario_rows[1:]
        if row["passes_current_guardrails"] == 1
        and _decimal(row["gross_profit_delta_vs_current_rub"]) > ZERO
    ]
    best_passing_scenario = (
        max(passing, key=lambda row: _decimal(row["economic_effect_rub"]))["scenario_id"]
        if passing
        else ""
    )
    _write_csv(args.output_dir / "scenario-summary.csv", scenario_rows)
    _write_csv(args.output_dir / "focus-loss-events.csv", detailed_loss_rows)
    _write_csv(args.output_dir / "regular-topup-audit.csv", audit_rows)
    artifacts = ["scenario-summary.csv", "focus-loss-events.csv", "regular-topup-audit.csv"]
    manifest.update(
        {
            "status": "complete",
            "best_passing_scenario": best_passing_scenario,
            "artifacts": artifacts,
            "artifact_sha256": {name: _sha256(args.output_dir / name) for name in artifacts},
        }
    )
    _atomic_write_json(manifest_path, manifest)
    return manifest


def _parse_args(argv: list[str] | None = None) -> Any:
    args = _parse_profit_recovery_args(argv)
    if args.output_dir == PROFIT_RECOVERY_OUTPUT_DIR:
        args.output_dir = DEFAULT_OUTPUT_DIR
    return args


def main(argv: list[str] | None = None) -> int:
    manifest = _run(_parse_args(argv))
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
