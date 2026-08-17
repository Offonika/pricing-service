"""Test acceleration only as a filter for an already proven P50 supply gap.

The task is diagnostic and read-only.  It compares the unchanged v19 control,
the unfiltered P50 hybrid rule and three pre-registered acceleration filters on
the same checksum-validated frozen inputs.
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
from tasks import report_display_auto_order_hybrid_gap_backtest as hybrid
from tasks import report_display_auto_order_risk_buffer_backtest as v23
from tasks.analyze_display_auto_order_quick_backtest import _prepare_inputs
from tasks.build_display_auto_order_dry_run import load_auto_order_policy
from tasks.display_auto_order_backtest_preflight import load_scenario_config

ZERO = Decimal("0")
CONTROL_ROLE = "control_v19"
BASE_ROLE = "hybrid_p50"


@dataclass(frozen=True)
class AccelerationFilter:
    role: str
    recent_days: int
    baseline_days: int
    min_recent_sales: Decimal
    rate_multiplier: Decimal


FILTERS = (
    AccelerationFilter("p50_acceleration_fast", 7, 28, Decimal("2"), Decimal("1.5")),
    AccelerationFilter("p50_acceleration_balanced", 14, 42, Decimal("2"), Decimal("1.5")),
    AccelerationFilter("p50_acceleration_strict", 14, 42, Decimal("3"), Decimal("2")),
)


def _filter_kwargs(profile: AccelerationFilter | None) -> dict[str, Any]:
    if profile is None:
        return {}
    return {
        "hybrid_gap_acceleration_recent_days": profile.recent_days,
        "hybrid_gap_acceleration_baseline_days": profile.baseline_days,
        "hybrid_gap_acceleration_min_recent_sales": profile.min_recent_sales,
        "hybrid_gap_acceleration_rate_multiplier": profile.rate_multiplier,
        "hybrid_gap_acceleration_require_forecast_growth": True,
    }


def _outcome_summary(rows: Sequence[Mapping[str, Any]], *, role: str) -> dict[str, Any]:
    exposed = [
        row
        for row in rows
        if frozen._clean(row.get("scenario_role")) == role
        and frozen._decimal(row.get("hybrid_order_component_qty")) > ZERO
    ]
    return {
        "exposed_sku_count": len(exposed),
        "service_gain_sku_count": sum(row["outcome"] == "service_gain" for row in exposed),
        "excess_without_gain_sku_count": sum(
            row["outcome"] == "excess_without_observed_gain" for row in exposed
        ),
        "no_material_effect_sku_count": sum(
            row["outcome"] == "no_material_inventory_effect" for row in exposed
        ),
    }


def _select_profile(period_comparison: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    indexed = {
        (frozen._clean(row.get("scenario_role")), frozen._clean(row.get("period"))): row
        for row in period_comparison
    }
    candidates: list[dict[str, Any]] = []
    for profile in FILTERS:
        pre = indexed[(profile.role, "pre_july")]
        final = indexed[(profile.role, "july")]
        candidates.append(
            {
                "scenario_role": profile.role,
                "pre_final_economic_contribution_delta_rub": pre["economic_contribution_delta_rub"],
                "final_month_economic_contribution_delta_rub": final[
                    "economic_contribution_delta_rub"
                ],
                "pre_final_served_observed_delta_qty": pre["served_observed_delta_qty"],
                "final_month_served_observed_delta_qty": final["served_observed_delta_qty"],
            }
        )
    selected = max(
        candidates,
        key=lambda row: (
            frozen._decimal(row["pre_final_economic_contribution_delta_rub"]),
            frozen._decimal(row["pre_final_served_observed_delta_qty"]),
            row["scenario_role"],
        ),
    )
    selected = dict(selected)
    selected["selection_period"] = "pre_july"
    selected["holdout_period"] = "july"
    selected["positive_on_holdout"] = bool(
        frozen._decimal(selected["final_month_economic_contribution_delta_rub"]) > ZERO
    )
    return selected


def _period_delta_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    roles: Sequence[str],
    annual_rate: Decimal,
) -> list[dict[str, Any]]:
    indexed = {
        (frozen._clean(row.get("scenario_role")), frozen._clean(row.get("period"))): row
        for row in rows
        if frozen._clean(row.get("strategy")) == "model"
    }
    output: list[dict[str, Any]] = []
    periods = sorted({key[1] for key in indexed if key[0] == CONTROL_ROLE})
    for role in roles:
        for period in periods:
            control = indexed[(CONTROL_ROLE, period)]
            candidate = indexed[(role, period)]
            days = (
                date.fromisoformat(frozen._clean(candidate["date_to"]))
                - date.fromisoformat(frozen._clean(candidate["date_from"]))
            ).days + 1
            served_delta = frozen._decimal(candidate["served_qty"]) - frozen._decimal(
                control["served_qty"]
            )
            observed_delta = frozen._decimal(candidate["served_observed_qty"]) - frozen._decimal(
                control["served_observed_qty"]
            )
            gross_profit_delta = frozen._decimal(candidate["gross_profit_rub"]) - frozen._decimal(
                control["gross_profit_rub"]
            )
            capital_delta = frozen._decimal(
                candidate["average_inventory_value_rub"]
            ) - frozen._decimal(control["average_inventory_value_rub"])
            carrying_cost_delta = capital_delta * annual_rate * Decimal(days) / frozen.YEAR_DAYS
            output.append(
                {
                    "scenario_role": role,
                    "period": period,
                    "date_from": candidate["date_from"],
                    "date_to": candidate["date_to"],
                    "served_delta_qty": str(served_delta),
                    "served_observed_delta_qty": str(observed_delta),
                    "gross_profit_delta_rub": str(gross_profit_delta),
                    "average_inventory_value_delta_rub": str(capital_delta),
                    "carrying_cost_delta_rub": str(carrying_cost_delta),
                    "economic_contribution_delta_rub": str(
                        gross_profit_delta - carrying_cost_delta
                    ),
                }
            )
    return output


def _manual_review_rows(
    decision_rows: Sequence[Mapping[str, Any]],
    *,
    names: Mapping[str, str],
    margins: Mapping[str, Decimal],
) -> list[dict[str, Any]]:
    fast_rows = {
        (
            frozen._clean(row.get("decision_date")),
            frozen._clean(row.get("nomenclature_code")),
        ): row
        for row in decision_rows
        if frozen._clean(row.get("scenario_role")) == "p50_acceleration_fast"
        and frozen._decimal(row.get("hybrid_gap_order_component_qty")) > ZERO
    }
    rows: list[dict[str, Any]] = []
    for row in decision_rows:
        if frozen._clean(row.get("scenario_role")) != BASE_ROLE:
            continue
        component = frozen._decimal(row.get("hybrid_gap_order_component_qty"))
        if component <= ZERO:
            continue
        code = frozen._clean(row.get("nomenclature_code"))
        decision_date = frozen._clean(row.get("decision_date"))
        fast_row = fast_rows.get((decision_date, code))
        unit_margin = max(ZERO, margins.get(code, ZERO))
        rows.append(
            {
                "decision_date": decision_date,
                "nomenclature_code": code,
                "name": names.get(code, ""),
                "status": row.get("status"),
                "recommended_contour": (
                    "shadow_auto_candidate" if fast_row else "manual_review_only"
                ),
                "fast_filter_passed": int(fast_row is not None),
                "fast_recommended_gap_qty": (
                    fast_row.get("hybrid_gap_order_component_qty") if fast_row else "0"
                ),
                "fast_recent_sales_qty": (
                    fast_row.get("hybrid_gap_acceleration_recent_sales_qty") if fast_row else ""
                ),
                "fast_baseline_sales_qty": (
                    fast_row.get("hybrid_gap_acceleration_baseline_sales_qty") if fast_row else ""
                ),
                "fast_recent_rate": (
                    fast_row.get("hybrid_gap_acceleration_recent_rate") if fast_row else ""
                ),
                "fast_baseline_rate": (
                    fast_row.get("hybrid_gap_acceleration_baseline_rate") if fast_row else ""
                ),
                "reason_code": "open_pipeline_blocks_coverable_p50_gap",
                "reason": (
                    "Открытая более поздняя партия оставляет доказуемый дефицит после "
                    "возможного прихода новой партии по P50"
                ),
                "model_stock_qty": row.get("model_stock_qty"),
                "reserve_qty": row.get("reserve_qty"),
                "model_pipeline_qty": row.get("model_pipeline_qty"),
                "inventory_position_qty": row.get("inventory_position_qty"),
                "forecast_rate_sales": row.get("forecast_rate_sales"),
                "hybrid_gap_demand_rate": row.get("hybrid_gap_demand_rate"),
                "new_arrival_date_p50": row.get("hybrid_gap_new_arrival_date"),
                "later_open_arrival_date": row.get("hybrid_gap_reliable_arrival_date"),
                "coverable_days": row.get("hybrid_gap_coverable_days"),
                "coverable_shortage_qty": row.get("hybrid_gap_coverable_shortage_qty"),
                "recommended_gap_qty": str(component),
                "estimated_gross_margin_at_risk_rub": str(component * unit_margin),
                "lead_time_confidence": row.get("lead_time_confidence"),
                "human_check": (
                    "Проверить исходную обещанную дату, переносы, частичный приход и отмену "
                    "открытой партии; затем подтвердить, уменьшить или отклонить количество"
                ),
                "production_action": "none_read_only",
            }
        )
    return sorted(
        rows,
        key=lambda row: (
            -frozen._decimal(row["estimated_gross_margin_at_risk_rub"]),
            row["decision_date"],
            row["nomenclature_code"],
        ),
    )


def _markdown(summary: Mapping[str, Any]) -> str:
    rows = {row["scenario_role"]: row for row in summary["comparison"]}
    lines = [
        "# P50-дефицит с фильтром ускорения",
        "",
        "Все варианты используют один frozen-набор и не создают реальных заказов.",
        "",
        "| Вариант | Продажи к контролю | Валовая прибыль | Средний капитал | Экономический вклад |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for role in (BASE_ROLE, *(profile.role for profile in FILTERS)):
        row = rows[role]
        lines.append(
            f"| {role} | {row['delta_to_control_served_observed_qty']} | "
            f"{row['delta_to_control_gross_profit_rub']} ₽ | "
            f"{row['delta_to_control_average_inventory_value_rub']} ₽ | "
            f"{row['delta_to_control_economic_contribution_rub']} ₽ |"
        )
    selected = summary["selection"]
    lines.extend(
        [
            "",
            "## Выбор без подглядывания в июль",
            "",
            f"По январю–июню выбран `{selected['scenario_role']}`. "
            f"Его экономический вклад на отдельном июле: "
            f"{selected['final_month_economic_contribution_delta_rub']} ₽.",
            "",
            "`manual-review-history.csv` — историческая read-only очередь спорных "
            "поставок. Она не является списком production-заказов.",
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
    v23_manifest = json.loads((source_v23_dir / "analysis-manifest.json").read_text())
    for name, expected in v23_manifest["files"].items():
        if frozen._sha256(source_v23_dir / name) != expected:
            raise ValueError(f"v23 checksum mismatch: {name}")
    v23_schedule = frozen._read_csv(source_v23_dir / "decision-buffer-schedule.csv")
    control_schedule = {
        (
            date.fromisoformat(frozen._clean(row["decision_date"])),
            frozen._clean(row["nomenclature_code"]),
        ): frozen._decimal(row["baseline_buffer_qty"])
        for row in v23_schedule
        if frozen._decimal(row["baseline_buffer_qty"]) > ZERO
    }
    inputs = _prepare_inputs(preflight_dir)
    quick_summary = json.loads(source_quick_summary_json.read_text(encoding="utf-8"))
    source = v23._source_scenario(quick_summary, inputs["frozen_scenarios"])
    policy = load_auto_order_policy(policy_json)
    config = load_scenario_config(scenario_config_json)
    shared_cache: dict[tuple[str, date, int], list[Decimal]] = {}
    simulation_args = {
        "fact_rows_by_date": inputs["fact_rows_by_date"],
        "decision_rows_by_date": inputs["decision_rows_by_date"],
        "initial_pipeline_rows": inputs["initial_pipeline"],
        "sales_by_code": inputs["sales_by_code"],
        "policy": policy,
        "config": config,
        "date_from": inputs["date_from"],
        "date_to": inputs["date_to"],
        "keep_detail": True,
        "keep_loss_detail": False,
        "hybrid_gap_detail_only": True,
        "demand_sample_cache": shared_cache,
        "decision_service_buffers": control_schedule,
    }
    definitions: list[tuple[str, str, AccelerationFilter | None]] = [
        (CONTROL_ROLE, "off", None),
        (BASE_ROLE, "p50", None),
        *((profile.role, "p50", profile) for profile in FILTERS),
    ]
    results: dict[str, frozen.SimulationResult] = {}
    for role, quantile, profile in definitions:
        results[role] = frozen.simulate_scenario(
            scenario=replace(source, scenario_id=f"{source.scenario_id}_{role}"),
            hybrid_gap_arrival_quantile=quantile,
            **_filter_kwargs(profile),
            **simulation_args,
        )

    period_days = (inputs["date_to"] - inputs["date_from"]).days + 1
    actual = frozen._summary(
        scenario=results[CONTROL_ROLE].scenario,
        strategy="actual",
        metrics=results[CONTROL_ROLE].actual,
        period_days=period_days,
    )
    actual["scenario_role"] = "actual"
    summaries: list[dict[str, Any]] = [actual]
    model_by_role: dict[str, dict[str, Any]] = {}
    for role, result in results.items():
        row = frozen._summary(
            scenario=result.scenario,
            strategy="model",
            metrics=result.model,
            period_days=period_days,
        )
        row["scenario_role"] = role
        row.update(result.diagnostics.as_summary_fields())
        summaries.append(row)
        model_by_role[role] = row
    comparison = [
        hybrid._delta_row(model_by_role[role], model_by_role[CONTROL_ROLE], scenario_role=role)
        for role, _, _ in definitions
    ]

    period_rows: list[dict[str, Any]] = []
    stage_rows: list[dict[str, Any]] = []
    decision_rows: list[dict[str, Any]] = []
    daily_rows: list[dict[str, Any]] = []
    sku_rows: list[dict[str, Any]] = []
    for role, result in results.items():
        period_rows.extend(
            {**row, "scenario_role": role}
            for row in frozen._period_summary_rows(
                result.daily_rows,
                scenario_id=result.scenario.scenario_id,
                date_from=inputs["date_from"],
                date_to=inputs["date_to"],
            )
        )
        stage_rows.extend(
            {**row, "scenario_role": role}
            for row in frozen._stage_summary_rows(result, period_days)
        )
        decision_rows.extend({**row, "scenario_role": role} for row in result.decision_rows)
        daily_rows.extend({**row, "scenario_role": role} for row in result.daily_rows)
        if role != CONTROL_ROLE:
            sku_rows.extend(
                hybrid._sku_delta_rows(
                    results[CONTROL_ROLE], result, scenario_role=role, period_days=period_days
                )
            )
    candidate_roles = [role for role, _, _ in definitions if role != CONTROL_ROLE]
    period_comparison = _period_delta_rows(
        period_rows,
        roles=candidate_roles,
        annual_rate=source.cost.total_annual_rate,
    )
    names = {
        frozen._clean(row.get("nomenclature_code")): frozen._clean(row.get("name"))
        for rows in inputs["decision_rows_by_date"].values()
        for row in rows
        if frozen._clean(row.get("nomenclature_code"))
    }
    margins = {
        frozen._clean(row.get("nomenclature_code")): frozen._decimal(
            row.get("gross_margin_per_unit_rub")
        )
        for rows in inputs["decision_rows_by_date"].values()
        for row in rows
        if frozen._clean(row.get("nomenclature_code"))
    }
    manual_rows = _manual_review_rows(decision_rows, names=names, margins=margins)
    summary: dict[str, Any] = {
        "schema": "display_auto_order_hybrid_acceleration_backtest.v1",
        "diagnostic_only": True,
        "production_authorized": False,
        "date_from": inputs["date_from"].isoformat(),
        "date_to": inputs["date_to"].isoformat(),
        "source_cohort_sku_count": 2662,
        "active_simulation_sku_count": len(results[CONTROL_ROLE].model),
        "method": {
            "control": "unchanged v19",
            "base_candidate": "unfiltered hybrid P50 coverable gap",
            "filters": [profile.__dict__ for profile in FILTERS],
            "forecast_growth_required": True,
            "selection_period": "pre_july",
            "holdout_period": "july",
            "no_look_ahead": True,
        },
        "scenario_summaries": summaries,
        "comparison": comparison,
        "period_comparison": period_comparison,
        "outcomes": {
            role: _outcome_summary(sku_rows, role=role)
            for role, _, _ in definitions
            if role != CONTROL_ROLE
        },
        "selection": _select_profile(period_comparison),
        "manual_review_history_row_count": len(manual_rows),
        "source_checksums": {
            "preflight_manifest": frozen._sha256(preflight_dir / "run-manifest.json"),
            "v23_manifest": frozen._sha256(source_v23_dir / "analysis-manifest.json"),
            "source_quick_summary": frozen._sha256(source_quick_summary_json),
            "policy_json": frozen._sha256(policy_json),
            "scenario_config_json": frozen._sha256(scenario_config_json),
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    artifacts: dict[str, Sequence[Mapping[str, Any]]] = {
        "scenario-summary.csv": summaries,
        "scenario-comparison.csv": comparison,
        "period-summary.csv": period_rows,
        "period-comparison.csv": period_comparison,
        "stage-summary.csv": stage_rows,
        "daily-summary.csv": daily_rows,
        "decision-detail.csv": decision_rows,
        "sku-comparison.csv": sku_rows,
        "manual-review-history.csv": manual_rows,
    }
    for name, rows in artifacts.items():
        frozen._write_csv(output_dir / name, rows)
    (output_dir / "analysis-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
    )
    (output_dir / "HYBRID-ACCELERATION-BACKTEST.md").write_text(
        _markdown(summary), encoding="utf-8"
    )
    filenames = [*artifacts, "analysis-summary.json", "HYBRID-ACCELERATION-BACKTEST.md"]
    manifest = {
        "schema": "display_auto_order_hybrid_acceleration_backtest_manifest.v1",
        "diagnostic_only": True,
        "production_authorized": False,
        "files": {name: frozen._sha256(output_dir / name) for name in filenames},
    }
    (output_dir / "analysis-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
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
