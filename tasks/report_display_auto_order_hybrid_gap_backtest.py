"""Compare the v23/v19 control with one dynamic hybrid shortage-gap rule.

The task reads only checksum-validated frozen inputs, runs the same full cohort
for control/P50/P75, and never creates production orders or external records.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from tasks import report_display_auto_order_frozen_backtest as frozen
from tasks import report_display_auto_order_risk_buffer_backtest as v23
from tasks.analyze_display_auto_order_quick_backtest import _prepare_inputs
from tasks.build_display_auto_order_dry_run import load_auto_order_policy
from tasks.display_auto_order_backtest_preflight import load_scenario_config

ZERO = Decimal("0")
CONTROL_ROLE = "control_v19"
P50_ROLE = "hybrid_p50"
P75_ROLE = "hybrid_p75"


def _delta_row(
    source: Mapping[str, Any],
    control: Mapping[str, Any],
    *,
    scenario_role: str,
) -> dict[str, Any]:
    columns = (
        "served_qty",
        "served_observed_qty",
        "lost_qty",
        "lost_observed_qty",
        "fill_rate",
        "observed_fill_rate",
        "gross_profit_rub",
        "average_inventory_value_rub",
        "economic_contribution_rub",
        "gmroi_annualized",
        "ending_inventory_qty",
        "order_qty",
        "order_value_rub",
    )
    row: dict[str, Any] = {"scenario_role": scenario_role}
    for column in columns:
        row[column] = source[column]
        row[f"delta_to_control_{column}"] = str(
            frozen._decimal(source[column]) - frozen._decimal(control[column])
        )
    return row


def _sku_delta_rows(
    control: frozen.SimulationResult,
    candidate: frozen.SimulationResult,
    *,
    scenario_role: str,
    period_days: int,
) -> list[dict[str, Any]]:
    hybrid_decisions: dict[str, dict[str, Decimal | int]] = {}
    for row in candidate.decision_rows:
        component = frozen._decimal(row.get("hybrid_gap_order_component_qty"))
        if component <= ZERO:
            continue
        code = frozen._clean(row.get("nomenclature_code"))
        values = hybrid_decisions.setdefault(code, {"decisions": 0, "quantity": ZERO})
        values["decisions"] = int(values["decisions"]) + 1
        values["quantity"] = frozen._decimal(values["quantity"]) + component

    rows: list[dict[str, Any]] = []
    for code in sorted(set(control.model) | set(candidate.model)):
        left = control.model.get(code, frozen.Metric())
        right = candidate.model.get(code, frozen.Metric())
        exposure = hybrid_decisions.get(code, {"decisions": 0, "quantity": ZERO})
        service_delta = right.served_observed_qty - left.served_observed_qty
        capital_delta = (right.inventory_value_days_rub - left.inventory_value_days_rub) / Decimal(
            period_days
        )
        ending_delta = right.ending_inventory_qty - left.ending_inventory_qty
        if service_delta > ZERO:
            outcome = "service_gain"
        elif capital_delta > ZERO or ending_delta > ZERO:
            outcome = "excess_without_observed_gain"
        elif frozen._decimal(exposure["quantity"]) > ZERO:
            outcome = "no_material_inventory_effect"
        else:
            outcome = "not_exposed"
        rows.append(
            {
                "scenario_role": scenario_role,
                "nomenclature_code": code,
                "hybrid_order_decision_count": int(exposure["decisions"]),
                "hybrid_order_component_qty": str(exposure["quantity"]),
                "outcome": outcome,
                "served_observed_delta_qty": str(service_delta),
                "gross_profit_delta_rub": str(right.gross_profit_rub - left.gross_profit_rub),
                "average_inventory_value_delta_rub": str(capital_delta),
                "ending_inventory_delta_qty": str(ending_delta),
                "order_delta_qty": str(right.order_qty - left.order_qty),
                "order_value_delta_rub": str(right.order_value_rub - left.order_value_rub),
            }
        )
    return rows


def _period_delta_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    annual_rate: Decimal,
) -> list[dict[str, Any]]:
    indexed = {
        (frozen._clean(row.get("scenario_role")), frozen._clean(row.get("period"))): row
        for row in rows
        if frozen._clean(row.get("strategy")) == "model"
    }
    output: list[dict[str, Any]] = []
    for role in (P50_ROLE, P75_ROLE):
        for period in sorted({key[1] for key in indexed if key[0] == role}):
            control = indexed[(CONTROL_ROLE, period)]
            candidate = indexed[(role, period)]
            days = (
                date.fromisoformat(frozen._clean(candidate["date_to"]))
                - date.fromisoformat(frozen._clean(candidate["date_from"]))
            ).days + 1
            served_delta = frozen._decimal(candidate["served_qty"]) - frozen._decimal(
                control["served_qty"]
            )
            served_observed_delta = frozen._decimal(
                candidate["served_observed_qty"]
            ) - frozen._decimal(control["served_observed_qty"])
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
                    "served_observed_delta_qty": str(served_observed_delta),
                    "gross_profit_delta_rub": str(gross_profit_delta),
                    "average_inventory_value_delta_rub": str(capital_delta),
                    "carrying_cost_delta_rub": str(carrying_cost_delta),
                    "economic_contribution_delta_rub": str(
                        gross_profit_delta - carrying_cost_delta
                    ),
                }
            )
    return output


def _markdown(summary: Mapping[str, Any]) -> str:
    rows = {row["scenario_role"]: row for row in summary["comparison"]}
    min_days = int(summary["method"].get("p50_min_coverable_days") or 0)
    filter_text = (
        f" Для P50 дополнительно требуется разрыв не короче {min_days} дней."
        if min_days > 0
        else ""
    )
    return f"""# Тест гибридного правила на замороженных исторических данных

## Итог

Контроль v19 сравнивается с одним динамическим правилом, где новая партия
моделируется по P50 и P75. Правило действует только на доказуемый дефицит между
приходом новой партии и ближайшим уже открытым приходом.{filter_text}

| Вариант | Записанные продажи к контролю | Валовая прибыль | Средний капитал | Конечный остаток | Экономический вклад |
| --- | ---: | ---: | ---: | ---: | ---: |
| P50 | {rows[P50_ROLE]['delta_to_control_served_observed_qty']} | {rows[P50_ROLE]['delta_to_control_gross_profit_rub']} ₽ | {rows[P50_ROLE]['delta_to_control_average_inventory_value_rub']} ₽ | {rows[P50_ROLE]['delta_to_control_ending_inventory_qty']} | {rows[P50_ROLE]['delta_to_control_economic_contribution_rub']} ₽ |
| P75 | {rows[P75_ROLE]['delta_to_control_served_observed_qty']} | {rows[P75_ROLE]['delta_to_control_gross_profit_rub']} ₽ | {rows[P75_ROLE]['delta_to_control_average_inventory_value_rub']} ₽ | {rows[P75_ROLE]['delta_to_control_ending_inventory_qty']} | {rows[P75_ROLE]['delta_to_control_economic_contribution_rub']} ₽ |

## Ограничение

Это диагностическая симуляция без production-заказов, forward shadow и внешних
записей. Замороженный набор не содержит полный журнал переносов обещанной даты
поставщиком; надёжный приход здесь означает положительную открытую модельную
партию, уже видимую в очереди на дату решения.
"""


def build_analysis(
    *,
    preflight_dir: Path,
    source_v23_dir: Path,
    source_quick_summary_json: Path,
    output_dir: Path,
    policy_json: Path,
    scenario_config_json: Path,
    p50_min_coverable_days: int = 0,
) -> dict[str, Any]:
    if p50_min_coverable_days < 0:
        raise ValueError("P50 minimum coverable days cannot be negative")
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
    results: dict[str, frozen.SimulationResult] = {}
    for role, quantile in (
        (CONTROL_ROLE, "off"),
        (P50_ROLE, "p50"),
        (P75_ROLE, "p75"),
    ):
        results[role] = frozen.simulate_scenario(
            scenario=replace(source, scenario_id=f"{source.scenario_id}_{role}"),
            hybrid_gap_arrival_quantile=quantile,
            hybrid_gap_min_coverable_days=(p50_min_coverable_days if role == P50_ROLE else 0),
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

    comparisons = [
        _delta_row(model_by_role[role], model_by_role[CONTROL_ROLE], scenario_role=role)
        for role in (CONTROL_ROLE, P50_ROLE, P75_ROLE)
    ]
    period_rows: list[dict[str, Any]] = []
    stage_rows: list[dict[str, Any]] = []
    decision_rows: list[dict[str, Any]] = []
    daily_rows: list[dict[str, Any]] = []
    sku_rows: list[dict[str, Any]] = []
    for role, result in results.items():
        period_rows.extend(
            {
                **row,
                "scenario_role": role,
            }
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
                _sku_delta_rows(
                    results[CONTROL_ROLE],
                    result,
                    scenario_role=role,
                    period_days=period_days,
                )
            )
    period_comparison = _period_delta_rows(
        period_rows,
        annual_rate=source.cost.total_annual_rate,
    )
    exposed = [row for row in sku_rows if frozen._decimal(row["hybrid_order_component_qty"]) > ZERO]
    summary: dict[str, Any] = {
        "schema": "display_auto_order_hybrid_gap_backtest.v1",
        "diagnostic_only": True,
        "production_authorized": False,
        "date_from": inputs["date_from"].isoformat(),
        "date_to": inputs["date_to"].isoformat(),
        "source_cohort_sku_count": 2662,
        "active_simulation_sku_count": len(results[CONTROL_ROLE].model),
        "source_scenario_id": source.scenario_id,
        "method": {
            "control": "unchanged v19 top-50 decision buffer on the v23 source scenario",
            "candidate": "dynamic sale-stage coverable shortage between candidate P50/P75 arrival and the nearest already-open later arrival",
            "demand_rate": "maximum of frozen forecast and completed 30/90/180 calendar-day rates; decision day excluded",
            "single_open_lot": True,
            "order_change_required": True,
            "p50_min_coverable_days": p50_min_coverable_days,
            "no_look_ahead": True,
        },
        "scenario_summaries": summaries,
        "comparison": comparisons,
        "period_comparison": period_comparison,
        "diagnostics": {
            role: {
                "positive_order_decisions": result.diagnostics.hybrid_gap_positive_order_decisions,
                "ordered_component_qty": str(result.diagnostics.hybrid_gap_order_component_qty),
                "exposed_sku_count": len(
                    {
                        frozen._clean(row["nomenclature_code"])
                        for row in exposed
                        if frozen._clean(row["scenario_role"]) == role
                    }
                ),
                "service_gain_sku_count": sum(
                    frozen._clean(row["scenario_role"]) == role
                    and frozen._clean(row["outcome"]) == "service_gain"
                    for row in exposed
                ),
                "excess_without_gain_sku_count": sum(
                    frozen._clean(row["scenario_role"]) == role
                    and frozen._clean(row["outcome"]) == "excess_without_observed_gain"
                    for row in exposed
                ),
            }
            for role, result in results.items()
            if role != CONTROL_ROLE
        },
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
        "scenario-comparison.csv": comparisons,
        "period-summary.csv": period_rows,
        "period-comparison.csv": period_comparison,
        "stage-summary.csv": stage_rows,
        "daily-summary.csv": daily_rows,
        "decision-detail.csv": decision_rows,
        "sku-comparison.csv": sku_rows,
    }
    for name, rows in artifacts.items():
        frozen._write_csv(output_dir / name, rows)
    (output_dir / "analysis-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / "HYBRID-GAP-BACKTEST.md").write_text(_markdown(summary), encoding="utf-8")
    filenames = [*artifacts, "analysis-summary.json", "HYBRID-GAP-BACKTEST.md"]
    manifest = {
        "schema": "display_auto_order_hybrid_gap_backtest_manifest.v1",
        "diagnostic_only": True,
        "production_authorized": False,
        "files": {name: frozen._sha256(output_dir / name) for name in filenames},
    }
    (output_dir / "analysis-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight-dir", type=Path, required=True)
    parser.add_argument("--source-v23-dir", type=Path, required=True)
    parser.add_argument("--source-quick-summary-json", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--p50-min-coverable-days", type=int, default=0)
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
        p50_min_coverable_days=args.p50_min_coverable_days,
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
