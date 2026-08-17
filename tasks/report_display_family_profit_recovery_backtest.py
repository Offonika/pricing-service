"""Diagnose and test targeted profit recovery for the iPhone 14 Pro Max family."""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from app.services.display_family_demand import (
    DisplayFamilyOrderAllocation,
    DisplayFamilyProfitProtection,
    allocate_display_family_order_pool,
    build_display_family_profit_protection,
)
from tasks.report_display_auto_order_frozen_backtest import _clean, _decimal
from tasks.report_display_family_demand_backtest import (
    _atomic_write_json,
    _prepare,
    _run_fingerprint,
    _sha256,
    _write_csv,
)
from tasks.report_display_family_order_pool_backtest import (
    DEFAULT_OUTPUT_DIR as ORDER_POOL_OUTPUT_DIR,
)
from tasks.report_display_family_order_pool_backtest import (
    _parse_args as _parse_order_pool_args,
)
from tasks.run_assortment_lifecycle_v2_economic_backtest import (
    _period_metrics,
    _simulate,
)

ZERO = Decimal("0")
CAPITAL_TIERS_RUB = (Decimal("100000"), Decimal("250000"), Decimal("500000"))
DEFAULT_OUTPUT_DIR = Path(
    "reports/assortment_lifecycle/backtest-2026-01-01_2026-07-31/"
    "display-family-profit-recovery-backtest-2026-08-15/targeted-iphone14pm"
)


def _available_dates(inputs: Any) -> dict[str, set[date]]:
    result: dict[str, set[date]] = {}
    for business_date, rows in inputs.fact_rows_by_date.items():
        for row in rows:
            code = _clean(row.get("nomenclature_code"))
            free_stock = max(
                ZERO,
                _decimal(row.get("physical_stock_qty"))
                - _decimal(row.get("effective_reserve_qty")),
            )
            if code and free_stock > ZERO:
                result.setdefault(code, set()).add(business_date)
    return result


def _actual_period_metrics(
    result: Any, *, period_from: date, period_to: date
) -> dict[str, Decimal]:
    rows = [
        row
        for row in result.daily_rows
        if period_from <= date.fromisoformat(str(row["business_date"])) <= period_to
    ]
    days = Decimal((period_to - period_from).days + 1)
    gross_profit = sum((_decimal(row.get("actual_gross_profit_rub")) for row in rows), ZERO)
    average_inventory = (
        sum((_decimal(row.get("actual_inventory_value_rub")) for row in rows), ZERO) / days
    )
    carrying_cost = (
        average_inventory * result.scenario.cost.total_annual_rate * days / Decimal("365")
    )
    gmroi = (
        gross_profit * Decimal("365") / days / average_inventory
        if average_inventory > ZERO
        else ZERO
    )
    return {
        "served_sales_qty": sum(
            (_decimal(row.get("actual_served_observed_qty")) for row in rows), ZERO
        ),
        "gross_profit_rub": gross_profit,
        "average_inventory_value_rub": average_inventory,
        "carrying_cost_rub": carrying_cost,
        "economic_effect_rub": gross_profit - carrying_cost,
        "gmroi": gmroi,
        "ending_inventory_qty": sum(
            (metric.ending_inventory_qty for metric in result.actual.values()), ZERO
        ),
    }


def _run_candidate(
    *,
    inputs: Any,
    scenario: Any,
    scenario_id: str,
    auto_order_policy: Any,
    scenario_config: Any,
    period_to: date,
    representation: Any,
    spike_keys: set[tuple[date, str]],
    spike_rates: Mapping[tuple[date, str], Decimal],
    overrides: Mapping[tuple[date, str], Decimal],
    topup_qty_overrides: Mapping[tuple[date, str], Decimal] | None = None,
    topup_arrival_date_overrides: Mapping[tuple[date, str], date] | None = None,
    keep_loss_detail: bool = False,
) -> Any:
    return _simulate(
        inputs=inputs,
        scenario=replace(scenario, scenario_id=scenario_id),
        policy=auto_order_policy,
        config=scenario_config,
        date_from=date(2026, 1, 1),
        date_to=period_to,
        representation_minimums=representation,
        spike_keys=spike_keys,
        spike_rates=spike_rates,
        demand_sample_cache={},
        keep_decision_detail=True,
        keep_loss_detail=keep_loss_detail,
        ordinary_order_overrides=overrides,
        ordinary_order_topup_qty_overrides=topup_qty_overrides,
        ordinary_order_topup_arrival_date_overrides=topup_arrival_date_overrides,
    )


def _scenario_row(
    *,
    scenario_id: str,
    metrics: Mapping[str, Decimal],
    current: Mapping[str, Decimal],
    actual: Mapping[str, Decimal],
) -> dict[str, Any]:
    capital_delta = metrics["average_inventory_value_rub"] - current["average_inventory_value_rub"]
    tiers = [str(int(tier)) for tier in CAPITAL_TIERS_RUB if capital_delta <= tier]
    passes = all(
        (
            metrics["served_sales_qty"] >= current["served_sales_qty"],
            metrics["gross_profit_rub"] >= current["gross_profit_rub"],
            metrics["economic_effect_rub"] >= current["economic_effect_rub"],
            metrics["gmroi"] >= current["gmroi"],
            metrics["ending_excess_stock_qty"] <= current["ending_excess_stock_qty"],
            capital_delta <= CAPITAL_TIERS_RUB[-1],
        )
    )
    return {
        "scenario_id": scenario_id,
        **{key: str(value) for key, value in metrics.items()},
        "gross_profit_shortfall_to_fact_rub": str(
            actual["gross_profit_rub"] - metrics["gross_profit_rub"]
        ),
        "served_sales_shortfall_to_fact_qty": str(
            actual["served_sales_qty"] - metrics["served_sales_qty"]
        ),
        "average_inventory_delta_vs_current_rub": str(capital_delta),
        "gross_profit_delta_vs_current_rub": str(
            metrics["gross_profit_rub"] - current["gross_profit_rub"]
        ),
        "economic_effect_delta_vs_current_rub": str(
            metrics["economic_effect_rub"] - current["economic_effect_rub"]
        ),
        "gmroi_delta_vs_current": str(metrics["gmroi"] - current["gmroi"]),
        "eligible_average_capital_tiers_rub": ";".join(tiers),
        "passes_current_guardrails": int(passes),
    }


def _loss_decomposition(
    loss_rows: Sequence[Mapping[str, Any]],
    *,
    period_from: date,
    period_to: date,
    allocation_audit: Sequence[DisplayFamilyOrderAllocation],
) -> list[dict[str, Any]]:
    blockers = {
        (row.decision_date.isoformat(), row.nomenclature_code): row.blocker
        for row in allocation_audit
    }
    aggregate: dict[tuple[str, str], dict[str, Decimal]] = {}
    for row in loss_rows:
        business_date = date.fromisoformat(_clean(row.get("business_date")))
        if not period_from <= business_date <= period_to:
            continue
        code = _clean(row.get("nomenclature_code"))
        prior_date = _clean(row.get("prior_evaluation_date"))
        allocation_blocker = blockers.get((prior_date, code), "")
        if allocation_blocker == "family_lot_still_open":
            reason = "family_open_lot_block"
        elif _clean(row.get("next_pipeline_arrival_date")):
            reason = "waiting_for_pipeline"
        elif _clean(row.get("prior_triggered")) in {"", "0"}:
            reason = "minmax_trigger_not_reached"
        elif _decimal(row.get("prior_recommended_order_qty")) <= ZERO:
            reason = "target_or_safety_too_low"
        else:
            reason = "lead_time_or_order_timing"
        key = (code, reason)
        target = aggregate.setdefault(
            key,
            {"lost_sales_qty": ZERO, "lost_gross_profit_rub": ZERO, "loss_days": ZERO},
        )
        lost = _decimal(row.get("lost_observed_qty"))
        target["lost_sales_qty"] += lost
        target["lost_gross_profit_rub"] += lost * _decimal(row.get("gross_margin_per_unit_rub"))
        target["loss_days"] += Decimal("1")
    return [
        {
            "nomenclature_code": code,
            "reason": reason,
            **{key: str(value) for key, value in values.items()},
        }
        for (code, reason), values in sorted(
            aggregate.items(), key=lambda item: item[1]["lost_gross_profit_rub"], reverse=True
        )
    ]


def _audit_dicts(rows: Sequence[Any]) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        payload = asdict(row)
        for key, value in payload.items():
            if isinstance(value, date):
                payload[key] = value.isoformat()
        result.append(payload)
    return result


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
    if set(members) != focus_codes:
        raise ValueError("profit recovery runner escaped the frozen focus family")
    period_to = policy_v2.periods.training_to
    if period_to >= date(2026, 7, 1):
        raise ValueError("profit recovery runner must not consume July holdout")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = args.output_dir / "run-manifest.json"
    identity = {
        "schema": "display_family_profit_recovery_backtest.v1",
        "focus_code": args.focus_code,
        "scope_sku_count": len(members),
        "training_to": period_to.isoformat(),
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
        scenario=replace(scenario, scenario_id=f"{scenario.scenario_id}-recovery-control"),
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
    available_dates = _available_dates(inputs)
    current_overrides, current_allocation = allocate_display_family_order_pool(
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
        scenario_id=f"{scenario.scenario_id}-recovery-current",
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
    scenario_rows = [
        _scenario_row(
            scenario_id="current-family-order-pool",
            metrics=current_metrics,
            current=current_metrics,
            actual=actual_metrics,
        )
    ]
    scenario_results: dict[str, tuple[dict[tuple[date, str], Decimal], Any]] = {}
    protection_audits: dict[str, Sequence[DisplayFamilyProfitProtection]] = {}

    availability_overrides, availability_audit = allocate_display_family_order_pool(
        control.decision_rows,
        members=members,
        sales_by_code=inputs.sales_by_code,
        available_dates_by_code=available_dates,
        availability_corrected=True,
        short_lookback_days=30,
        long_lookback_days=90,
        max_share_step=Decimal("0.10"),
        capital_cap_fraction=ZERO,
        one_open_family_lot=True,
    )
    scenario_results["availability-corrected"] = (availability_overrides, availability_audit)

    current_open_keys = {
        (row.decision_date, row.nomenclature_code)
        for row in current_allocation
        if row.blocker == "family_lot_still_open"
    }
    current_open_keys.update(
        (
            date.fromisoformat(_clean(row.get("decision_date"))),
            _clean(row.get("nomenclature_code")),
        )
        for row in control.decision_rows
        if _decimal(row.get("model_pipeline_qty")) > ZERO
    )
    for unit_cap in (1, 2, 3):
        safety_id = f"profit-safety-u{unit_cap}"
        safety_overrides, safety_audit = build_display_family_profit_protection(
            control.decision_rows,
            base_overrides=current_overrides,
            mode="safety",
            annual_carrying_rate=scenario.cost.total_annual_rate,
            max_units_per_decision=unit_cap,
            gmroi_hurdle=current_metrics["gmroi"],
        )
        scenario_results[safety_id] = (safety_overrides, safety_audit)
        protection_audits[safety_id] = safety_audit
        topup_id = f"pipeline-topup-u{unit_cap}"
        topup_overrides, topup_audit = build_display_family_profit_protection(
            control.decision_rows,
            base_overrides=current_overrides,
            mode="pipeline_topup",
            annual_carrying_rate=scenario.cost.total_annual_rate,
            max_units_per_decision=unit_cap,
            gmroi_hurdle=current_metrics["gmroi"],
            open_lot_keys=current_open_keys,
        )
        scenario_results[topup_id] = (topup_overrides, topup_audit)
        protection_audits[topup_id] = topup_audit

    metrics_by_id: dict[str, Mapping[str, Decimal]] = {}
    for scenario_id, (overrides, _audit) in scenario_results.items():
        manifest["status"] = f"running_{scenario_id}"
        _atomic_write_json(manifest_path, manifest)
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
        )
        metrics = _period_metrics(
            result,
            period_from=policy_v2.periods.training_from,
            period_to=period_to,
        )
        metrics_by_id[scenario_id] = metrics
        scenario_rows.append(
            _scenario_row(
                scenario_id=scenario_id,
                metrics=metrics,
                current=current_metrics,
                actual=actual_metrics,
            )
        )

    passing_ids = {
        row["scenario_id"]
        for row in scenario_rows
        if row["scenario_id"] != "current-family-order-pool"
        and row["passes_current_guardrails"] == 1
        and _decimal(row["gross_profit_delta_vs_current_rub"]) > ZERO
    }
    best_passing_scenario = (
        max(passing_ids, key=lambda value: metrics_by_id[value]["economic_effect_rub"])
        if passing_ids
        else ""
    )
    selected_components: list[str] = []
    combined_overrides = current_overrides
    combined_allocation = current_allocation
    if "availability-corrected" in passing_ids:
        combined_overrides = availability_overrides
        combined_allocation = availability_audit
        selected_components.append("availability-corrected")
    for prefix, mode in (("profit-safety", "safety"), ("pipeline-topup", "pipeline_topup")):
        candidates = [scenario_id for scenario_id in passing_ids if scenario_id.startswith(prefix)]
        if not candidates:
            continue
        selected = max(candidates, key=lambda value: metrics_by_id[value]["economic_effect_rub"])
        unit_cap = int(selected.rsplit("u", 1)[1])
        open_keys = {
            (row.decision_date, row.nomenclature_code)
            for row in combined_allocation
            if row.blocker == "family_lot_still_open"
        }
        if mode == "pipeline_topup":
            open_keys.update(current_open_keys)
        combined_overrides, combined_audit = build_display_family_profit_protection(
            control.decision_rows,
            base_overrides=combined_overrides,
            mode=mode,
            annual_carrying_rate=scenario.cost.total_annual_rate,
            max_units_per_decision=unit_cap,
            gmroi_hurdle=current_metrics["gmroi"],
            open_lot_keys=open_keys,
        )
        protection_audits[f"combined-{selected}"] = combined_audit
        selected_components.append(selected)

    combined_result = _run_candidate(
        inputs=inputs,
        scenario=scenario,
        scenario_id=f"{scenario.scenario_id}-combined",
        auto_order_policy=auto_order_policy,
        scenario_config=scenario_config,
        period_to=period_to,
        representation=representation,
        spike_keys=spike_keys,
        spike_rates=spike_rates,
        overrides=combined_overrides,
    )
    combined_metrics = _period_metrics(
        combined_result,
        period_from=policy_v2.periods.training_from,
        period_to=period_to,
    )
    scenario_rows.append(
        {
            **_scenario_row(
                scenario_id="combined",
                metrics=combined_metrics,
                current=current_metrics,
                actual=actual_metrics,
            ),
            "selected_components": ";".join(selected_components),
        }
    )

    loss_rows = _loss_decomposition(
        current_result.loss_rows,
        period_from=policy_v2.periods.training_from,
        period_to=period_to,
        allocation_audit=current_allocation,
    )
    _atomic_write_json(args.output_dir / "actual-metrics.json", actual_metrics)
    _atomic_write_json(
        args.output_dir / "current-comparison.json",
        {
            "actual": actual_metrics,
            "current": current_metrics,
            "deltas": {
                "served_sales_delta_qty": str(
                    current_metrics["served_sales_qty"] - actual_metrics["served_sales_qty"]
                ),
                "gross_profit_delta_rub": str(
                    current_metrics["gross_profit_rub"] - actual_metrics["gross_profit_rub"]
                ),
                "economic_effect_delta_rub": str(
                    current_metrics["economic_effect_rub"] - actual_metrics["economic_effect_rub"]
                ),
                "gmroi_delta": str(current_metrics["gmroi"] - actual_metrics["gmroi"]),
                "ending_excess_stock_delta_qty": "not_applicable_for_actual",
            },
        },
    )
    _write_csv(args.output_dir / "scenario-summary.csv", scenario_rows)
    _write_csv(args.output_dir / "residual-loss-decomposition.csv", loss_rows)
    _write_csv(
        args.output_dir / "current-family-allocation-audit.csv",
        _audit_dicts(current_allocation),
    )
    _write_csv(
        args.output_dir / "availability-family-allocation-audit.csv",
        _audit_dicts(availability_audit),
    )
    protection_rows = []
    for scenario_id, rows in protection_audits.items():
        protection_rows.extend({"scenario_id": scenario_id, **row} for row in _audit_dicts(rows))
    _write_csv(args.output_dir / "profit-protection-audit.csv", protection_rows)
    artifacts = [
        "actual-metrics.json",
        "current-comparison.json",
        "scenario-summary.csv",
        "residual-loss-decomposition.csv",
        "current-family-allocation-audit.csv",
        "availability-family-allocation-audit.csv",
        "profit-protection-audit.csv",
    ]
    combined_row = scenario_rows[-1]
    manifest.update(
        {
            "status": "complete",
            "selected_components": selected_components,
            "best_passing_scenario": best_passing_scenario,
            "combined_passes_guardrails": bool(combined_row["passes_current_guardrails"]),
            "artifacts": artifacts,
            "artifact_sha256": {name: _sha256(args.output_dir / name) for name in artifacts},
        }
    )
    _atomic_write_json(manifest_path, manifest)
    return manifest


def _parse_args(argv: list[str] | None = None) -> Any:
    args = _parse_order_pool_args(argv)
    argv = argv or []
    if "--max-share-step" not in argv:
        args.max_share_step = Decimal("0.10")
    if "--output-dir" not in argv and args.output_dir == ORDER_POOL_OUTPUT_DIR:
        args.output_dir = DEFAULT_OUTPUT_DIR
    return args


def main(argv: list[str] | None = None) -> int:
    manifest = _run(_parse_args(argv))
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
