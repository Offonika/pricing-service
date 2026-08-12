"""Run one full frozen replay for the selected v22 decision-buffer candidate.

The task compares the immutable v19 top-50 service buffer with the same buffer
plus the economically ranked v22 increment.  It reads only checksum-validated
frozen artifacts, never queries production systems, and never creates orders.
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
from tasks.analyze_display_auto_order_pipeline_lot_reliability import (
    PROFILES,
    top_share_buffers,
)
from tasks.analyze_display_auto_order_quick_backtest import _prepare_inputs
from tasks.build_display_auto_order_dry_run import load_auto_order_policy
from tasks.display_auto_order_backtest_preflight import load_scenario_config

ZERO = Decimal("0")
SELECTED_STRATEGY = "economic_extra_0.50"
BASELINE_ROLE = "baseline_v19"
CANDIDATE_ROLE = "candidate_v22"


def _validated_manifest(
    directory: Path,
    *,
    required_files: Sequence[str],
) -> dict[str, Any]:
    manifest_path = directory / "analysis-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_files = manifest.get("files") or {}
    for name in required_files:
        expected = frozen._clean(expected_files.get(name))
        if not expected or frozen._sha256(directory / name) != expected:
            raise ValueError(f"analysis checksum mismatch: {directory.name}/{name}")
    return manifest


def build_buffer_schedules(
    risk_rows: Sequence[Mapping[str, Any]],
    allocation_rows: Sequence[Mapping[str, Any]],
    *,
    selected_strategy: str = SELECTED_STRATEGY,
) -> tuple[
    dict[tuple[date, str], Decimal],
    dict[tuple[date, str], Decimal],
    list[dict[str, Any]],
]:
    prepared: list[dict[str, Any]] = []
    source_by_key: dict[tuple[date, str], Mapping[str, Any]] = {}
    for source in risk_rows:
        row = dict(source)
        row["baseline_v19_score"] = frozen._clean(row.get("shortage_expected_qty"))
        decision_date = date.fromisoformat(frozen._clean(row.get("decision_date")))
        code = frozen._clean(row.get("nomenclature_code"))
        key = (decision_date, code)
        if not code or key in source_by_key:
            raise ValueError(f"invalid or duplicate risk opportunity: {key}")
        source_by_key[key] = row
        prepared.append(row)

    baseline_profile = next(profile for profile in PROFILES if profile.name == "baseline_v19")
    baseline_by_id = top_share_buffers(prepared, baseline_profile)
    baseline: dict[tuple[date, str], Decimal] = {}
    id_to_key: dict[str, tuple[date, str]] = {}
    for row in prepared:
        opportunity_id = frozen._clean(row.get("opportunity_id"))
        key = (
            date.fromisoformat(frozen._clean(row.get("decision_date"))),
            frozen._clean(row.get("nomenclature_code")),
        )
        id_to_key[opportunity_id] = key
        quantity = baseline_by_id.get(opportunity_id, ZERO)
        if quantity > ZERO:
            baseline[key] = quantity

    candidate = dict(baseline)
    extra_by_key: dict[tuple[date, str], Decimal] = {}
    seen_allocations: set[tuple[date, str]] = set()
    for row in allocation_rows:
        if frozen._clean(row.get("strategy")) != selected_strategy:
            continue
        allocated = max(ZERO, frozen._decimal(row.get("allocated_extra_qty")))
        key = (
            date.fromisoformat(frozen._clean(row.get("decision_date"))),
            frozen._clean(row.get("nomenclature_code")),
        )
        if key in seen_allocations:
            raise ValueError(f"duplicate economic allocation: {key}")
        seen_allocations.add(key)
        if key not in source_by_key:
            raise ValueError(f"economic allocation has no risk opportunity: {key}")
        expected_baseline = baseline.get(key, ZERO)
        rendered_baseline = frozen._decimal(row.get("baseline_buffer_qty"))
        if rendered_baseline != expected_baseline:
            raise ValueError(f"economic allocation baseline mismatch: {key}")
        if allocated > ZERO:
            extra_by_key[key] = allocated
            candidate[key] = expected_baseline + allocated

    schedule_rows: list[dict[str, Any]] = []
    for key in sorted(set(baseline) | set(candidate)):
        decision_date, code = key
        schedule_rows.append(
            {
                "decision_date": decision_date.isoformat(),
                "nomenclature_code": code,
                "baseline_buffer_qty": str(baseline.get(key, ZERO)),
                "economic_extra_qty": str(extra_by_key.get(key, ZERO)),
                "candidate_buffer_qty": str(candidate.get(key, ZERO)),
                "selected_strategy": selected_strategy,
            }
        )
    return baseline, candidate, schedule_rows


def _source_scenario(
    quick_summary: Mapping[str, Any],
    scenarios: Sequence[frozen.FrozenScenario],
) -> frozen.FrozenScenario:
    source_roles = quick_summary["source_scenario_roles"]
    selection = frozen.select_scenarios(
        scenarios,
        run_mode=frozen.RUN_MODE_QUICK,
        control_scenario_id=source_roles["control"],
        hypothesis_scenario_id=source_roles["hypothesis"],
        cautious_scenario_id=source_roles["cautious"],
    )
    base = quick_summary["quick_base_pipeline"]
    selection = frozen.apply_quick_base_pipeline_profiles(
        selection,
        hypothesis_profile=frozen._clean(base.get("hypothesis_profile")),
        cautious_profile=frozen._clean(base.get("cautious_profile")),
    )
    by_id = {scenario.scenario_id: scenario for scenario in selection.scenarios}
    selected_id = frozen._clean(quick_summary["scenario_roles"].get("cautious"))
    if selected_id not in by_id:
        raise ValueError("quick summary P75 source scenario cannot be reconstructed")
    return by_id[selected_id]


def _delta_row(
    source: Mapping[str, Any],
    reference: Mapping[str, Any],
    *,
    comparison: str,
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
    row: dict[str, Any] = {"comparison": comparison}
    for column in columns:
        row[f"source_{column}"] = source[column]
        row[f"reference_{column}"] = reference[column]
        row[f"delta_{column}"] = str(
            frozen._decimal(source[column]) - frozen._decimal(reference[column])
        )
    return row


def _sku_candidate_delta_rows(
    baseline: frozen.SimulationResult,
    candidate: frozen.SimulationResult,
    *,
    period_days: int,
    first_decision_by_code: Mapping[str, Mapping[str, Any]],
    schedule_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    extra_by_code: dict[str, dict[str, Decimal | int]] = {}
    for row in schedule_rows:
        extra_qty = frozen._decimal(row.get("economic_extra_qty"))
        if extra_qty <= ZERO:
            continue
        code = frozen._clean(row.get("nomenclature_code"))
        values = extra_by_code.setdefault(code, {"decisions": 0, "quantity": ZERO})
        values["decisions"] = int(values["decisions"]) + 1
        values["quantity"] = frozen._decimal(values["quantity"]) + extra_qty
    rows: list[dict[str, Any]] = []
    for code in sorted(set(baseline.model) | set(candidate.model)):
        left = baseline.model.get(code, frozen.Metric())
        right = candidate.model.get(code, frozen.Metric())
        source = first_decision_by_code.get(code, {})
        extra = extra_by_code.get(code, {"decisions": 0, "quantity": ZERO})
        served_observed_delta = right.served_observed_qty - left.served_observed_qty
        served_hidden_delta = right.served_hidden_qty - left.served_hidden_qty
        capital_delta = (right.inventory_value_days_rub - left.inventory_value_days_rub) / Decimal(
            period_days
        )
        ending_delta = right.ending_inventory_qty - left.ending_inventory_qty
        if served_observed_delta > ZERO:
            outcome = "service_gain"
        elif served_hidden_delta < ZERO:
            outcome = "hidden_service_loss"
        elif capital_delta > ZERO or ending_delta > ZERO:
            outcome = "excess_without_observed_gain"
        elif frozen._decimal(extra["quantity"]) > ZERO:
            outcome = "no_material_inventory_effect"
        else:
            outcome = "not_exposed"
        rows.append(
            {
                "nomenclature_code": code,
                "name": frozen._clean(source.get("name")),
                "status_at_first_decision": frozen._clean(source.get("status")),
                "inventory_cost_per_unit_rub": frozen._clean(
                    source.get("inventory_cost_per_unit_rub")
                ),
                "gross_margin_per_unit_rub": frozen._clean(source.get("gross_margin_per_unit_rub")),
                "economic_extra_decision_count": int(extra["decisions"]),
                "economic_extra_schedule_qty": str(extra["quantity"]),
                "outcome": outcome,
                "served_observed_delta_qty": str(served_observed_delta),
                "served_hidden_delta_qty": str(served_hidden_delta),
                "lost_observed_delta_qty": str(right.lost_observed_qty - left.lost_observed_qty),
                "gross_profit_delta_rub": str(right.gross_profit_rub - left.gross_profit_rub),
                "average_inventory_value_delta_rub": str(capital_delta),
                "ending_inventory_delta_qty": str(ending_delta),
                "order_delta_qty": str(right.order_qty - left.order_qty),
                "order_value_delta_rub": str(right.order_value_rub - left.order_value_rub),
            }
        )
    rows.sort(
        key=lambda row: (
            -abs(frozen._decimal(row["gross_profit_delta_rub"])),
            frozen._clean(row["nomenclature_code"]),
        )
    )
    return rows


def _period_comparison_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    annual_rate: Decimal,
) -> list[dict[str, Any]]:
    index = {
        (frozen._clean(row.get("scenario_role")), frozen._clean(row.get("period"))): row
        for row in rows
        if frozen._clean(row.get("strategy")) == "model"
    }
    output: list[dict[str, Any]] = []
    for period in sorted({key[1] for key in index}):
        baseline = index[(BASELINE_ROLE, period)]
        candidate = index[(CANDIDATE_ROLE, period)]
        days = (
            date.fromisoformat(frozen._clean(candidate.get("date_to")))
            - date.fromisoformat(frozen._clean(candidate.get("date_from")))
        ).days + 1
        served_delta = frozen._decimal(candidate.get("served_qty")) - frozen._decimal(
            baseline.get("served_qty")
        )
        gross_profit_delta = frozen._decimal(candidate.get("gross_profit_rub")) - frozen._decimal(
            baseline.get("gross_profit_rub")
        )
        capital_delta = frozen._decimal(
            candidate.get("average_inventory_value_rub")
        ) - frozen._decimal(baseline.get("average_inventory_value_rub"))
        carrying_cost_delta = capital_delta * annual_rate * Decimal(days) / frozen.YEAR_DAYS
        output.append(
            {
                "period": period,
                "date_from": candidate["date_from"],
                "date_to": candidate["date_to"],
                "served_delta_qty": str(served_delta),
                "gross_profit_delta_rub": str(gross_profit_delta),
                "average_inventory_value_delta_rub": str(capital_delta),
                "carrying_cost_delta_rub": str(carrying_cost_delta),
                "economic_contribution_delta_rub": str(gross_profit_delta - carrying_cost_delta),
            }
        )
    return output


def _markdown(summary: Mapping[str, Any]) -> str:
    headline = summary["headline"]
    acceptance = summary["acceptance"]
    result = "ПРОШЁЛ" if acceptance["passed"] else "НЕ ПРОШЁЛ"
    return f"""# Полный frozen-backtest risk-buffer v23

## Итог

Строгий acceptance: **{result}**.

Кандидат v22 относительно базовой защиты v19:

- записанные продажи: {headline['served_observed_delta_to_baseline_qty']} ед.;
- валовая прибыль: {headline['gross_profit_delta_to_baseline_rub']} ₽;
- средний складской капитал: {headline['capital_delta_to_baseline_rub']} ₽;
- GMROI: {headline['gmroi_delta_to_baseline']};
- конечный остаток: {headline['ending_inventory_delta_to_baseline_qty']} ед.

Вся связка v19+v22 относительно исходного v16-P75 вернула
{headline['served_observed_delta_to_source_qty']} записанных продаж, но изменила
экономический вклад на {headline['economic_contribution_delta_to_source_rub']} ₽.

Относительно исторического факта у кандидата остаётся
{headline['lost_observed_vs_actual_qty']} ед. потерянной записанной продажи.

## Как применён профиль

Контроль сохраняет top-50 буферы v19. Кандидат сохраняет те же буферы и добавляет
только экономически ранжированную половину lot-age/acceleration добавки v22.
Буфер повышает min и max с даты решения до следующего недельного решения SKU.
Будущие продажи и будущие приходы при формировании расписания не используются.

## Ограничение

Это полный replay заказов на frozen-данных, но не production и не forward shadow.
Никакие реальные заказы и внешние записи не создавались.
"""


def build_analysis(
    *,
    preflight_dir: Path,
    risk_analysis_dir: Path,
    economic_analysis_dir: Path,
    source_quick_summary_json: Path,
    output_dir: Path,
    policy_json: Path,
    scenario_config_json: Path,
) -> dict[str, Any]:
    _validated_manifest(
        risk_analysis_dir,
        required_files=("risk-scores.csv",),
    )
    economic_manifest = _validated_manifest(
        economic_analysis_dir,
        required_files=(
            "analysis-summary.json",
            "economic-extra-allocation.csv",
        ),
    )
    economic_summary = json.loads(
        (economic_analysis_dir / "analysis-summary.json").read_text(encoding="utf-8")
    )
    selected = economic_summary["economic_screening"]["selected_on_pre_july"]
    selected_strategy = frozen._clean(selected.get("strategy"))
    if selected_strategy != SELECTED_STRATEGY:
        raise ValueError(f"unexpected selected economic strategy: {selected_strategy}")

    risk_rows = frozen._read_csv(risk_analysis_dir / "risk-scores.csv")
    allocation_rows = frozen._read_csv(economic_analysis_dir / "economic-extra-allocation.csv")
    baseline_schedule, candidate_schedule, schedule_rows = build_buffer_schedules(
        risk_rows,
        allocation_rows,
        selected_strategy=selected_strategy,
    )

    inputs = _prepare_inputs(preflight_dir)
    quick_summary = json.loads(source_quick_summary_json.read_text(encoding="utf-8"))
    source = _source_scenario(quick_summary, inputs["frozen_scenarios"])
    source_reference = next(
        row
        for row in quick_summary["quick_comparison"]
        if frozen._clean(row.get("scenario_id")) == source.scenario_id
    )
    baseline_scenario = replace(
        source,
        scenario_id=f"{source.scenario_id}_riskbuffer_v19",
    )
    candidate_scenario = replace(
        source,
        scenario_id=f"{source.scenario_id}_riskbuffer_v22_extra050",
    )
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
        "demand_sample_cache": shared_cache,
    }
    baseline = frozen.simulate_scenario(
        scenario=baseline_scenario,
        decision_service_buffers=baseline_schedule,
        **simulation_args,
    )
    candidate = frozen.simulate_scenario(
        scenario=candidate_scenario,
        decision_service_buffers=candidate_schedule,
        **simulation_args,
    )

    period_days = (inputs["date_to"] - inputs["date_from"]).days + 1
    actual_summary = frozen._summary(
        scenario=baseline.scenario,
        strategy="actual",
        metrics=baseline.actual,
        period_days=period_days,
    )
    baseline_summary = frozen._summary(
        scenario=baseline.scenario,
        strategy="model",
        metrics=baseline.model,
        period_days=period_days,
    )
    candidate_summary = frozen._summary(
        scenario=candidate.scenario,
        strategy="model",
        metrics=candidate.model,
        period_days=period_days,
    )
    actual_summary["scenario_role"] = "actual"
    baseline_summary["scenario_role"] = BASELINE_ROLE
    candidate_summary["scenario_role"] = CANDIDATE_ROLE
    baseline_summary.update(baseline.diagnostics.as_summary_fields())
    candidate_summary.update(candidate.diagnostics.as_summary_fields())

    acceptance = frozen._acceptance_result(actual_summary, candidate_summary)
    baseline_acceptance = frozen._acceptance_result(actual_summary, baseline_summary)
    comparisons = [
        _delta_row(
            baseline_summary,
            actual_summary,
            comparison="baseline_v19_minus_actual",
        ),
        _delta_row(
            candidate_summary,
            actual_summary,
            comparison="candidate_v22_minus_actual",
        ),
        _delta_row(
            candidate_summary,
            baseline_summary,
            comparison="candidate_v22_minus_baseline_v19",
        ),
    ]
    candidate_vs_baseline = comparisons[-1]

    daily_rows = [{**row, "scenario_role": BASELINE_ROLE} for row in baseline.daily_rows] + [
        {**row, "scenario_role": CANDIDATE_ROLE} for row in candidate.daily_rows
    ]
    decision_rows = [{**row, "scenario_role": BASELINE_ROLE} for row in baseline.decision_rows] + [
        {**row, "scenario_role": CANDIDATE_ROLE} for row in candidate.decision_rows
    ]
    loss_rows = [{**row, "scenario_role": BASELINE_ROLE} for row in baseline.loss_rows] + [
        {**row, "scenario_role": CANDIDATE_ROLE} for row in candidate.loss_rows
    ]
    stage_rows = [
        {**row, "scenario_role": BASELINE_ROLE}
        for row in frozen._stage_summary_rows(baseline, period_days)
    ] + [
        {**row, "scenario_role": CANDIDATE_ROLE}
        for row in frozen._stage_summary_rows(candidate, period_days)
    ]
    period_rows = [
        {**row, "scenario_role": BASELINE_ROLE}
        for row in frozen._period_summary_rows(
            baseline.daily_rows,
            scenario_id=baseline.scenario.scenario_id,
            date_from=inputs["date_from"],
            date_to=inputs["date_to"],
        )
    ] + [
        {**row, "scenario_role": CANDIDATE_ROLE}
        for row in frozen._period_summary_rows(
            candidate.daily_rows,
            scenario_id=candidate.scenario.scenario_id,
            date_from=inputs["date_from"],
            date_to=inputs["date_to"],
        )
    ]
    period_comparison = _period_comparison_rows(
        period_rows,
        annual_rate=source.cost.total_annual_rate,
    )
    sku_rows = _sku_candidate_delta_rows(
        baseline,
        candidate,
        period_days=period_days,
        first_decision_by_code=inputs["first_decision_by_code"],
        schedule_rows=schedule_rows,
    )
    review_rows = [
        row for row in sku_rows if frozen._decimal(row.get("economic_extra_schedule_qty")) > ZERO
    ]

    headline = {
        "served_observed_delta_to_baseline_qty": candidate_vs_baseline["delta_served_observed_qty"],
        "gross_profit_delta_to_baseline_rub": candidate_vs_baseline["delta_gross_profit_rub"],
        "capital_delta_to_baseline_rub": candidate_vs_baseline["delta_average_inventory_value_rub"],
        "gmroi_delta_to_baseline": candidate_vs_baseline["delta_gmroi_annualized"],
        "ending_inventory_delta_to_baseline_qty": candidate_vs_baseline[
            "delta_ending_inventory_qty"
        ],
        "lost_observed_vs_actual_qty": candidate_summary["lost_observed_qty"],
        "served_observed_delta_to_source_qty": str(
            frozen._decimal(candidate_summary["served_observed_qty"])
            - frozen._decimal(source_reference["served_observed_qty"])
        ),
        "gross_profit_delta_to_source_rub": str(
            frozen._decimal(candidate_summary["gross_profit_rub"])
            - frozen._decimal(source_reference["gross_profit_rub"])
        ),
        "capital_delta_to_source_rub": str(
            frozen._decimal(candidate_summary["average_inventory_value_rub"])
            - frozen._decimal(source_reference["average_inventory_value_rub"])
        ),
        "economic_contribution_delta_to_source_rub": str(
            frozen._decimal(candidate_summary["economic_contribution_rub"])
            - frozen._decimal(source_reference["economic_contribution_rub"])
        ),
    }
    summary: dict[str, Any] = {
        "schema": "display_auto_order_risk_buffer_frozen_backtest.v1",
        "diagnostic_only": True,
        "production_authorized": False,
        "date_from": inputs["date_from"].isoformat(),
        "date_to": inputs["date_to"].isoformat(),
        "source_scenario_id": source.scenario_id,
        "selected_strategy": selected_strategy,
        "method": {
            "baseline": "v19 top-50 expected-shortage service buffers",
            "candidate": "baseline plus the selected v22 economic_extra_0.50 allocation",
            "application": "buffer raises min and max from its frozen decision date until the next decision for the SKU",
            "no_look_ahead": "schedule uses only checksum-validated v19/v22 decision artifacts; simulation consumes no future outcome when applying a decision",
            "acceptance": "gross profit and fill rate not below actual, and average inventory capital lower or annualized GMROI higher",
        },
        "schedule": {
            "baseline_positive_decisions": len(baseline_schedule),
            "candidate_positive_decisions": len(candidate_schedule),
            "economic_extra_positive_decisions": sum(
                frozen._decimal(row.get("economic_extra_qty")) > ZERO for row in schedule_rows
            ),
            "baseline_buffer_qty": str(sum(baseline_schedule.values(), ZERO)),
            "candidate_buffer_qty": str(sum(candidate_schedule.values(), ZERO)),
            "economic_extra_qty": str(
                sum(
                    (frozen._decimal(row.get("economic_extra_qty")) for row in schedule_rows),
                    ZERO,
                )
            ),
        },
        "acceptance": acceptance,
        "baseline_acceptance": baseline_acceptance,
        "headline": headline,
        "diagnostics": {
            "economic_extra_sku_count": len(review_rows),
            "sku_with_order_change_count": sum(
                frozen._decimal(row.get("order_delta_qty")) != ZERO for row in review_rows
            ),
            "sku_with_observed_service_gain_count": sum(
                frozen._decimal(row.get("served_observed_delta_qty")) > ZERO for row in review_rows
            ),
            "sku_with_excess_without_observed_gain_count": sum(
                frozen._clean(row.get("outcome")) == "excess_without_observed_gain"
                for row in review_rows
            ),
        },
        "period_comparison": period_comparison,
        "source_v16_reference": source_reference,
        "scenario_summaries": [actual_summary, baseline_summary, candidate_summary],
        "comparisons": comparisons,
        "source_checksums": {
            "preflight_manifest": frozen._sha256(preflight_dir / "run-manifest.json"),
            "risk_manifest": frozen._sha256(risk_analysis_dir / "analysis-manifest.json"),
            "economic_manifest": frozen._sha256(economic_analysis_dir / "analysis-manifest.json"),
            "economic_manifest_schema": economic_manifest.get("schema"),
            "source_quick_summary": frozen._sha256(source_quick_summary_json),
            "policy_json": frozen._sha256(policy_json),
            "scenario_config_json": frozen._sha256(scenario_config_json),
        },
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    frozen._write_csv(output_dir / "decision-buffer-schedule.csv", schedule_rows)
    frozen._write_csv(
        output_dir / "scenario-summary.csv",
        [actual_summary, baseline_summary, candidate_summary],
    )
    frozen._write_csv(output_dir / "scenario-comparison.csv", comparisons)
    frozen._write_csv(output_dir / "period-summary.csv", period_rows)
    frozen._write_csv(output_dir / "period-comparison.csv", period_comparison)
    frozen._write_csv(output_dir / "stage-summary.csv", stage_rows)
    frozen._write_csv(output_dir / "daily-summary.csv", daily_rows)
    frozen._write_csv(output_dir / "decision-detail.csv", decision_rows)
    frozen._write_csv(output_dir / "loss-detail.csv", loss_rows)
    frozen._write_csv(output_dir / "sku-candidate-vs-baseline.csv", sku_rows)
    frozen._write_csv(output_dir / "manual-review.csv", review_rows)
    (output_dir / "analysis-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / "RISK-BUFFER-FROZEN-BACKTEST.md").write_text(
        _markdown(summary),
        encoding="utf-8",
    )
    artifacts = (
        "analysis-summary.json",
        "decision-buffer-schedule.csv",
        "scenario-summary.csv",
        "scenario-comparison.csv",
        "period-summary.csv",
        "period-comparison.csv",
        "stage-summary.csv",
        "daily-summary.csv",
        "decision-detail.csv",
        "loss-detail.csv",
        "sku-candidate-vs-baseline.csv",
        "manual-review.csv",
        "RISK-BUFFER-FROZEN-BACKTEST.md",
    )
    manifest = {
        "schema": "display_auto_order_risk_buffer_frozen_backtest_manifest.v1",
        "diagnostic_only": True,
        "production_authorized": False,
        "files": {name: frozen._sha256(output_dir / name) for name in artifacts},
    }
    (output_dir / "analysis-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight-dir", type=Path, required=True)
    parser.add_argument("--risk-analysis-dir", type=Path, required=True)
    parser.add_argument("--economic-analysis-dir", type=Path, required=True)
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
        risk_analysis_dir=args.risk_analysis_dir,
        economic_analysis_dir=args.economic_analysis_dir,
        source_quick_summary_json=args.source_quick_summary_json,
        output_dir=args.output_dir,
        policy_json=args.auto_order_policy_json,
        scenario_config_json=args.scenario_config_json,
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
