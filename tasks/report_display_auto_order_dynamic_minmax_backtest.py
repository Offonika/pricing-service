"""Test dynamic min/max for stock still above the ordinary reorder point.

The task uses checksum-validated frozen inputs only.  It compares the unchanged
v19 control with three pre-registered acceleration profiles and never writes to
production systems or creates real orders.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from tasks import report_display_auto_order_frozen_backtest as frozen
from tasks import report_display_auto_order_hybrid_acceleration_backtest as acceleration
from tasks import report_display_auto_order_hybrid_gap_backtest as hybrid
from tasks import report_display_auto_order_risk_buffer_backtest as v23
from tasks.analyze_display_auto_order_quick_backtest import _prepare_inputs
from tasks.build_display_auto_order_dry_run import load_auto_order_policy
from tasks.display_auto_order_backtest_preflight import load_scenario_config

ZERO = Decimal("0")
CONTROL_ROLE = "control_v19"
UNLIMITED_SHADOW_BUDGET_RUB = Decimal("1000000000000")
DYNAMIC_STATUSES = ("sales_start", "sale", "working")


@dataclass(frozen=True)
class DynamicMinMaxProfile:
    role: str
    recent_days: int
    baseline_days: int
    min_recent_sales: Decimal
    rate_multiplier: Decimal
    lead_quantile: str


PROFILES = (
    DynamicMinMaxProfile("dynamic_fast_p50", 7, 28, Decimal("2"), Decimal("1.5"), "p50"),
    DynamicMinMaxProfile("dynamic_balanced_p50", 14, 42, Decimal("2"), Decimal("1.5"), "p50"),
    DynamicMinMaxProfile("dynamic_fast_p75", 7, 28, Decimal("2"), Decimal("1.5"), "p75"),
)


def _dynamic_scenario(
    source: frozen.FrozenScenario,
    profile: DynamicMinMaxProfile,
) -> frozen.FrozenScenario:
    return replace(
        source,
        scenario_id=f"{source.scenario_id}_{profile.role}",
        grow_acceleration_profile=profile.role,
        grow_acceleration_quantity_policy="dynamic_minmax_shortage",
        grow_acceleration_recent_days=profile.recent_days,
        grow_acceleration_baseline_days=profile.baseline_days,
        grow_acceleration_min_recent_sales=profile.min_recent_sales,
        grow_acceleration_rate_multiplier=profile.rate_multiplier,
        grow_acceleration_sku_cap_rub=ZERO,
        grow_acceleration_stage_budget_rub=UNLIMITED_SHADOW_BUDGET_RUB,
        grow_acceleration_medium_pipeline_fraction=source.base_pipeline_medium_fraction,
        grow_acceleration_low_pipeline_fraction=source.base_pipeline_low_fraction,
        grow_acceleration_require_forecast_growth=True,
        grow_acceleration_min_shortage_qty=Decimal("1"),
        grow_acceleration_cap_to_projected_shortage=True,
        grow_acceleration_single_open_lot=True,
    )


def _select_profile(period_comparison: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    indexed = {
        (frozen._clean(row.get("scenario_role")), frozen._clean(row.get("period"))): row
        for row in period_comparison
    }
    candidates: list[dict[str, Any]] = []
    for profile in PROFILES:
        pre = indexed[(profile.role, "pre_july")]
        holdout = indexed[(profile.role, "july")]
        candidates.append(
            {
                "scenario_role": profile.role,
                "pre_july_economic_contribution_delta_rub": pre["economic_contribution_delta_rub"],
                "pre_july_served_observed_delta_qty": pre["served_observed_delta_qty"],
                "july_economic_contribution_delta_rub": holdout["economic_contribution_delta_rub"],
                "july_served_observed_delta_qty": holdout["served_observed_delta_qty"],
            }
        )
    selected = max(
        candidates,
        key=lambda row: (
            frozen._decimal(row["pre_july_economic_contribution_delta_rub"]),
            frozen._decimal(row["pre_july_served_observed_delta_qty"]),
            row["scenario_role"],
        ),
    )
    selected = dict(selected)
    selected["selection_period"] = "pre_july"
    selected["holdout_period"] = "july"
    selected["positive_on_holdout"] = bool(
        frozen._decimal(selected["july_economic_contribution_delta_rub"]) > ZERO
    )
    return selected


def _shadow_rows(
    decision_rows: Sequence[Mapping[str, Any]],
    *,
    selected_role: str,
    names: Mapping[str, str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in decision_rows:
        component = frozen._decimal(row.get("acceleration_order_component_qty"))
        if frozen._clean(row.get("scenario_role")) != selected_role or component <= ZERO:
            continue
        rows.append(
            {
                "decision_date": row.get("decision_date"),
                "nomenclature_code": row.get("nomenclature_code"),
                "name": names.get(frozen._clean(row.get("nomenclature_code")), ""),
                "status": row.get("status"),
                "reason_code": "stock_above_min_accelerating_shortage",
                "reason": (
                    "Свободный запас ещё выше обычного min, но ускорившийся спрос "
                    "создаёт ожидаемый дефицит до следующего цикла поставки"
                ),
                "ordinary_min_stock_qty": row.get("acceleration_static_min_stock_qty"),
                "free_stock_qty": row.get("acceleration_free_stock_qty"),
                "recent_sales_qty": row.get("acceleration_recent_sales_qty"),
                "baseline_sales_qty": row.get("acceleration_baseline_sales_qty"),
                "recent_rate": row.get("acceleration_recent_rate"),
                "baseline_rate": row.get("acceleration_baseline_rate"),
                "lead_quantile": row.get("acceleration_lead_quantile"),
                "projected_demand_qty": row.get("acceleration_projected_demand_to_p75_qty"),
                "inventory_position_qty": row.get("acceleration_guard_inventory_position_qty"),
                "projected_shortage_qty": row.get("acceleration_projected_shortage_to_p75_qty"),
                "dynamic_minmax_increment_qty": str(component),
                "human_check": (
                    "Сверить ускорение, остаток, открытые партии и реальную следующую "
                    "возможность поставки; подтвердить, уменьшить или отклонить"
                ),
                "production_action": "none_read_only",
            }
        )
    return sorted(
        rows,
        key=lambda row: (
            -frozen._decimal(row["projected_shortage_qty"]),
            row["decision_date"],
            row["nomenclature_code"],
        ),
    )


def _markdown(summary: Mapping[str, Any]) -> str:
    comparisons = {row["scenario_role"]: row for row in summary["comparison"]}
    lines = [
        "# Dynamic min/max для запаса выше обычного min",
        "",
        "Все варианты рассчитаны на frozen-истории и не создают реальных заказов.",
        "",
        "| Профиль | Продажи | Валовая прибыль | Средний капитал | GMROI | Экономический вклад |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for profile in PROFILES:
        row = comparisons[profile.role]
        lines.append(
            f"| {profile.role} | {row['delta_to_control_served_observed_qty']} | "
            f"{row['delta_to_control_gross_profit_rub']} ₽ | "
            f"{row['delta_to_control_average_inventory_value_rub']} ₽ | "
            f"{row['delta_to_control_gmroi_annualized']} | "
            f"{row['delta_to_control_economic_contribution_rub']} ₽ |"
        )
    selection = summary["selection"]
    lines.extend(
        [
            "",
            "## Выбор и holdout",
            "",
            f"По периоду до июля выбран `{selection['scenario_role']}`. На июльском "
            f"holdout экономический вклад равен "
            f"{selection['july_economic_contribution_delta_rub']} ₽.",
            "",
            "`shadow-recommendations.csv` — только очередь для проверки человеком; "
            "`production_action` всегда равен `none_read_only`.",
        ]
    )
    return "\n".join(lines) + "\n"


def build_analysis(
    *,
    preflight_dir: Path,
    source_v23_dir: Path,
    source_quick_summary_json: Path,
    output_dir: Path,
    policy_json: Path,
    scenario_config_json: Path,
) -> dict[str, Any]:
    frozen.validate_preflight_directory(preflight_dir)
    manifest = json.loads((source_v23_dir / "analysis-manifest.json").read_text())
    for name, expected in manifest["files"].items():
        if frozen._sha256(source_v23_dir / name) != expected:
            raise ValueError(f"v23 checksum mismatch: {name}")
    control_schedule = {
        (
            date.fromisoformat(frozen._clean(row["decision_date"])),
            frozen._clean(row["nomenclature_code"]),
        ): frozen._decimal(row["baseline_buffer_qty"])
        for row in frozen._read_csv(source_v23_dir / "decision-buffer-schedule.csv")
        if frozen._decimal(row["baseline_buffer_qty"]) > ZERO
    }
    inputs = _prepare_inputs(preflight_dir)
    quick_summary = json.loads(source_quick_summary_json.read_text(encoding="utf-8"))
    source = v23._source_scenario(quick_summary, inputs["frozen_scenarios"])
    policy = load_auto_order_policy(policy_json)
    config = load_scenario_config(scenario_config_json)
    demand_sample_cache: dict[tuple[str, date, int], list[Decimal]] = {}
    shared = {
        "fact_rows_by_date": inputs["fact_rows_by_date"],
        "decision_rows_by_date": inputs["decision_rows_by_date"],
        "initial_pipeline_rows": inputs["initial_pipeline"],
        "sales_by_code": inputs["sales_by_code"],
        "policy": policy,
        "config": config,
        "date_from": inputs["date_from"],
        "date_to": inputs["date_to"],
        "keep_detail": True,
        "keep_decision_detail": False,
        "keep_loss_detail": False,
        "decision_service_buffers": control_schedule,
        "demand_sample_cache": demand_sample_cache,
    }
    results = {
        CONTROL_ROLE: frozen.simulate_scenario(
            scenario=replace(source, scenario_id=f"{source.scenario_id}_{CONTROL_ROLE}"),
            **shared,
        )
    }
    for profile in PROFILES:
        results[profile.role] = frozen.simulate_scenario(
            scenario=_dynamic_scenario(source, profile),
            acceleration_require_stock_above_min=True,
            acceleration_allowed_statuses=DYNAMIC_STATUSES,
            acceleration_lead_quantile=profile.lead_quantile,
            **shared,
        )

    period_days = (inputs["date_to"] - inputs["date_from"]).days + 1
    summaries: list[dict[str, Any]] = []
    model_by_role: dict[str, dict[str, Any]] = {}
    period_rows: list[dict[str, Any]] = []
    sku_rows: list[dict[str, Any]] = []
    for role, result in results.items():
        summary_row = frozen._summary(
            scenario=result.scenario,
            strategy="model",
            metrics=result.model,
            period_days=period_days,
        )
        summary_row["scenario_role"] = role
        summary_row.update(result.diagnostics.as_summary_fields())
        summaries.append(summary_row)
        model_by_role[role] = summary_row
        period_rows.extend(
            {**row, "scenario_role": role}
            for row in frozen._period_summary_rows(
                result.daily_rows,
                scenario_id=result.scenario.scenario_id,
                date_from=inputs["date_from"],
                date_to=inputs["date_to"],
            )
        )
        if role != CONTROL_ROLE:
            sku_rows.extend(
                hybrid._sku_delta_rows(
                    results[CONTROL_ROLE], result, scenario_role=role, period_days=period_days
                )
            )
    comparison = [
        hybrid._delta_row(
            model_by_role[profile.role], model_by_role[CONTROL_ROLE], scenario_role=profile.role
        )
        for profile in PROFILES
    ]
    period_comparison = acceleration._period_delta_rows(
        period_rows,
        roles=[profile.role for profile in PROFILES],
        annual_rate=source.cost.total_annual_rate,
    )
    selection = _select_profile(period_comparison)
    selected_profile = next(
        profile for profile in PROFILES if profile.role == selection["scenario_role"]
    )
    selected_detail = frozen.simulate_scenario(
        scenario=_dynamic_scenario(source, selected_profile),
        acceleration_require_stock_above_min=True,
        acceleration_allowed_statuses=DYNAMIC_STATUSES,
        acceleration_lead_quantile=selected_profile.lead_quantile,
        acceleration_detail_only=True,
        **{**shared, "keep_decision_detail": True},
    )
    decision_rows = [
        {**row, "scenario_role": selected_profile.role} for row in selected_detail.decision_rows
    ]
    names = {
        frozen._clean(row.get("nomenclature_code")): frozen._clean(row.get("name"))
        for rows in inputs["decision_rows_by_date"].values()
        for row in rows
        if frozen._clean(row.get("nomenclature_code"))
    }
    shadow_rows = _shadow_rows(
        decision_rows,
        selected_role=selection["scenario_role"],
        names=names,
    )
    summary: dict[str, Any] = {
        "schema": "display_auto_order_dynamic_minmax_backtest.v1",
        "diagnostic_only": True,
        "production_authorized": False,
        "date_from": inputs["date_from"].isoformat(),
        "date_to": inputs["date_to"].isoformat(),
        "method": {
            "segment": "stock_above_min_at_last_chance",
            "profiles": [profile.__dict__ for profile in PROFILES],
            "quantity": "projected shortage through lead time plus order cadence",
            "allowed_statuses": list(DYNAMIC_STATUSES),
            "forecast_growth_required": True,
            "single_open_dynamic_lot": True,
            "selection_period": "pre_july",
            "holdout_period": "july",
            "no_look_ahead": True,
        },
        "scenario_summaries": summaries,
        "comparison": comparison,
        "period_comparison": period_comparison,
        "selection": selection,
        "shadow_recommendation_row_count": len(shadow_rows),
        "source_checksums": {
            "preflight_manifest": frozen._sha256(preflight_dir / "run-manifest.json"),
            "v23_manifest": frozen._sha256(source_v23_dir / "analysis-manifest.json"),
            "source_quick_summary": frozen._sha256(source_quick_summary_json),
            "policy_json": frozen._sha256(policy_json),
            "scenario_config_json": frozen._sha256(scenario_config_json),
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    artifacts = {
        "scenario-summary.csv": summaries,
        "scenario-comparison.csv": comparison,
        "period-summary.csv": period_rows,
        "period-comparison.csv": period_comparison,
        "decision-detail.csv": decision_rows,
        "sku-comparison.csv": sku_rows,
        "shadow-recommendations.csv": shadow_rows,
    }
    for name, rows in artifacts.items():
        frozen._write_csv(output_dir / name, rows)
    (output_dir / "analysis-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    (output_dir / "DYNAMIC-MINMAX-BACKTEST.md").write_text(_markdown(summary), encoding="utf-8")
    filenames = [*artifacts, "analysis-summary.json", "DYNAMIC-MINMAX-BACKTEST.md"]
    analysis_manifest = {
        "schema": "display_auto_order_dynamic_minmax_backtest_manifest.v1",
        "diagnostic_only": True,
        "production_authorized": False,
        "files": {name: frozen._sha256(output_dir / name) for name in filenames},
    }
    (output_dir / "analysis-manifest.json").write_text(
        json.dumps(analysis_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight-dir", type=Path, required=True)
    parser.add_argument("--source-v23-dir", type=Path, required=True)
    parser.add_argument("--source-quick-summary-json", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
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
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    summary = build_analysis(
        preflight_dir=args.preflight_dir,
        source_v23_dir=args.source_v23_dir,
        source_quick_summary_json=args.source_quick_summary_json,
        output_dir=args.output_dir,
        policy_json=args.auto_order_policy_json,
        scenario_config_json=args.scenario_config_json,
    )
    print(json.dumps(summary, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
