"""Segment the mild base-pipeline display auto-order challenger.

The task replays the frozen control and cautious base-pipeline scenario, then
decomposes their SKU-level deltas using only pre-period or current-decision
features. It never queries production sources or creates purchase orders.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

import nbformat
from nbclient import NotebookClient

from tasks import report_display_auto_order_frozen_backtest as frozen
from tasks.analyze_display_auto_order_quick_backtest import (
    _period_rows,
    _prepare_inputs,
    _sku_rows,
)
from tasks.build_display_auto_order_dry_run import load_auto_order_policy
from tasks.display_auto_order_backtest_preflight import load_scenario_config

ZERO = Decimal("0")
ONE = Decimal("1")

PATTERN_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("all", ()),
    ("sparse", ("intermittent", "lumpy")),
    ("intermittent", ("intermittent",)),
    ("smooth", ("smooth",)),
    ("cold_start", ("no_history", "insufficient_history")),
)
COST_CAPS = (ZERO, Decimal("500"), Decimal("1500"), Decimal("3000"))
MARGIN_COST_RATIO_FLOORS = (ZERO, Decimal("0.25"), Decimal("0.5"), ONE)
P75_CAPS = (0, 60, 90)
STAGE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("all", ()),
    ("grow", ("sale",)),
)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(_clean(value) or "0")
    except (ArithmeticError, ValueError):
        return ZERO


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8-sig")
        return
    columns: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                columns.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ratio_band(value: Decimal) -> str:
    if value < ZERO:
        return "negative"
    if value < Decimal("0.25"):
        return "<0.25"
    if value < Decimal("0.5"):
        return "0.25-0.49"
    if value < ONE:
        return "0.50-0.99"
    return ">=1"


def enrich_sku_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for source in rows:
        row = dict(source)
        cost = _decimal(row.get("inventory_cost_per_unit_rub_start"))
        margin = _decimal(row.get("gross_margin_per_unit_rub_start"))
        ratio = margin / cost if cost > ZERO else ZERO
        row["gross_margin_to_cost_ratio_start"] = str(ratio)
        row["gross_margin_to_cost_ratio_band_start"] = _ratio_band(ratio)
        row["pipeline_profile_affected"] = int(
            _decimal(row.get("order_delta_to_control_qty")) != ZERO
            or _decimal(row.get("capital_delta_to_control_rub")) != ZERO
            or _decimal(row.get("served_observed_delta_to_control_qty")) != ZERO
        )
        output.append(row)
    return output


def _matches_rule(
    row: Mapping[str, Any],
    *,
    patterns: Sequence[str],
    cost_cap: Decimal,
    ratio_floor: Decimal,
    p75_cap: int,
    stages: Sequence[str],
) -> bool:
    if _clean(row.get("lead_time_confidence_start")) != "medium":
        return False
    if patterns and _clean(row.get("demand_pattern_preperiod")) not in patterns:
        return False
    if stages and _clean(row.get("stage_at_period_start")) not in stages:
        return False
    cost = _decimal(row.get("inventory_cost_per_unit_rub_start"))
    if cost_cap > ZERO and not (ZERO < cost < cost_cap):
        return False
    ratio = _decimal(row.get("gross_margin_to_cost_ratio_start"))
    if ratio < ratio_floor:
        return False
    p75 = int(_decimal(row.get("lead_time_p75_days_start")))
    if p75_cap > 0 and not (0 < p75 <= p75_cap):
        return False
    return True


def candidate_rule_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for pattern_name, patterns in PATTERN_RULES:
        for cost_cap in COST_CAPS:
            for ratio_floor in MARGIN_COST_RATIO_FLOORS:
                for p75_cap in P75_CAPS:
                    for stage_name, stages in STAGE_RULES:
                        eligible = [
                            row
                            for row in rows
                            if _matches_rule(
                                row,
                                patterns=patterns,
                                cost_cap=cost_cap,
                                ratio_floor=ratio_floor,
                                p75_cap=p75_cap,
                                stages=stages,
                            )
                        ]
                        if not eligible:
                            continue
                        served = sum(
                            (
                                _decimal(row.get("served_observed_delta_to_control_qty"))
                                for row in eligible
                            ),
                            ZERO,
                        )
                        gross_profit = sum(
                            (
                                _decimal(row.get("gross_profit_delta_to_control_rub"))
                                for row in eligible
                            ),
                            ZERO,
                        )
                        capital = sum(
                            (_decimal(row.get("capital_delta_to_control_rub")) for row in eligible),
                            ZERO,
                        )
                        economic = sum(
                            (
                                _decimal(row.get("economic_contribution_delta_to_control_rub"))
                                for row in eligible
                            ),
                            ZERO,
                        )
                        positive_economic = sorted(
                            (
                                max(
                                    ZERO,
                                    _decimal(row.get("economic_contribution_delta_to_control_rub")),
                                )
                                for row in eligible
                            ),
                            reverse=True,
                        )
                        positive_total = sum(positive_economic, ZERO)
                        output.append(
                            {
                                "rule_id": (
                                    f"medium95_{stage_name}_{pattern_name}_"
                                    f"cost{_clean(cost_cap) or 'none'}_"
                                    f"ratio{_clean(ratio_floor)}_p75{p75_cap or 'none'}"
                                ),
                                "stage_profile": stage_name,
                                "allowed_stages": ",".join(stages),
                                "pattern_profile": pattern_name,
                                "allowed_demand_patterns": ",".join(patterns),
                                "max_unit_cost_rub_exclusive": str(cost_cap),
                                "min_gross_margin_to_cost_ratio": str(ratio_floor),
                                "max_p75_days": p75_cap,
                                "sku_count": len(eligible),
                                "affected_sku_count": sum(
                                    int(row.get("pipeline_profile_affected") or 0)
                                    for row in eligible
                                ),
                                "sales_gain_sku_count": sum(
                                    _decimal(row.get("served_observed_delta_to_control_qty")) > ZERO
                                    for row in eligible
                                ),
                                "served_observed_delta_qty": str(served),
                                "gross_profit_delta_rub": str(gross_profit),
                                "capital_delta_rub": str(capital),
                                "ending_inventory_delta_qty": str(
                                    sum(
                                        (
                                            _decimal(
                                                row.get("ending_inventory_delta_to_control_qty")
                                            )
                                            for row in eligible
                                        ),
                                        ZERO,
                                    )
                                ),
                                "order_delta_qty": str(
                                    sum(
                                        (
                                            _decimal(row.get("order_delta_to_control_qty"))
                                            for row in eligible
                                        ),
                                        ZERO,
                                    )
                                ),
                                "economic_contribution_delta_rub": str(economic),
                                "gross_profit_per_capital_delta": str(
                                    gross_profit / capital if capital > ZERO else ZERO
                                ),
                                "top_1_share_of_positive_economic": str(
                                    positive_economic[0] / positive_total
                                    if positive_total > ZERO
                                    else ZERO
                                ),
                            }
                        )
    output.sort(
        key=lambda row: (
            -_decimal(row["economic_contribution_delta_rub"]),
            -_decimal(row["served_observed_delta_qty"]),
            _clean(row["rule_id"]),
        )
    )
    return output


def segment_effect_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    dimensions = (
        "stage_at_period_start",
        "demand_pattern_preperiod",
        "cost_band",
        "margin_band",
        "gross_margin_to_cost_ratio_band_start",
        "lead_time_band",
        "lead_time_confidence_start",
        "velocity_band_preperiod",
    )
    output: list[dict[str, Any]] = []
    for dimension in dimensions:
        grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[_clean(row.get(dimension)) or "unknown"].append(row)
        for value, group in grouped.items():
            output.append(
                {
                    "segment_dimension": dimension,
                    "segment_value": value,
                    "sku_count": len(group),
                    "affected_sku_count": sum(
                        int(row.get("pipeline_profile_affected") or 0) for row in group
                    ),
                    "served_observed_delta_qty": str(
                        sum(
                            (
                                _decimal(row.get("served_observed_delta_to_control_qty"))
                                for row in group
                            ),
                            ZERO,
                        )
                    ),
                    "gross_profit_delta_rub": str(
                        sum(
                            (
                                _decimal(row.get("gross_profit_delta_to_control_rub"))
                                for row in group
                            ),
                            ZERO,
                        )
                    ),
                    "capital_delta_rub": str(
                        sum(
                            (_decimal(row.get("capital_delta_to_control_rub")) for row in group),
                            ZERO,
                        )
                    ),
                    "economic_contribution_delta_rub": str(
                        sum(
                            (
                                _decimal(row.get("economic_contribution_delta_to_control_rub"))
                                for row in group
                            ),
                            ZERO,
                        )
                    ),
                }
            )
    output.sort(
        key=lambda row: (
            _clean(row["segment_dimension"]),
            -_decimal(row["economic_contribution_delta_rub"]),
        )
    )
    return output


def _build_markdown(summary: Mapping[str, Any]) -> str:
    headline = summary["headline"]
    top = summary["top_candidate_rules"][:10]
    lines = [
        "# Сегментация мягкого pipeline 95%",
        "",
        "## Короткий вывод",
        "",
        "Replay воспроизводит quick-v14 на уровне SKU и показывает, какие заранее "
        "определимые сегменты сохраняют продажи без общего роста неликвида.",
        "",
        f"Общий профиль вернул `{headline['served_observed_delta_qty']}` продажи, "
        f"но дал `{headline['economic_contribution_delta_rub']}` ₽ экономического "
        "вклада относительно контроля.",
        "",
        "## Лучшие диагностические правила",
        "",
        "| Правило | SKU | Продажи | Капитал, ₽ | Вклад, ₽ |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for row in top:
        lines.append(
            "| {rule} | {sku} | {sales} | {capital} | {economic} |".format(
                rule=row["rule_id"],
                sku=row["sku_count"],
                sales=row["served_observed_delta_qty"],
                capital=row["capital_delta_rub"],
                economic=row["economic_contribution_delta_rub"],
            )
        )
    lines.extend(
        [
            "",
            "Это диагностическое ранжирование, а не production-разрешение. Выбранное "
            "правило должно пройти отдельный causal quick-v15; выбирать SKU по их "
            "будущему результату запрещено.",
        ]
    )
    return "\n".join(lines) + "\n"


def _build_notebook(output_dir: Path, summary: Mapping[str, Any]) -> Path:
    notebook = nbformat.v4.new_notebook()
    notebook.metadata["kernelspec"] = {
        "display_name": "Python 3",
        "language": "python",
        "name": "python3",
    }
    notebook.cells = [
        nbformat.v4.new_markdown_cell(
            "## tl;dr\n\n"
            f"Мягкий pipeline 95%: продажи `{summary['headline']['served_observed_delta_qty']}`, "
            f"экономический вклад `{summary['headline']['economic_contribution_delta_rub']}` ₽. "
            "Ни один отдельный SKU по будущему результату не отбирается."
        ),
        nbformat.v4.new_markdown_cell(
            "## Context & Methods\n\n"
            "Период: 1 февраля — 31 июля 2026. Сравнение: frozen-контроль против "
            "medium-confidence pipeline 95%.\n\n"
            "### Key Assumptions\n\n"
            "Сегменты определяются по состоянию на начало периода или параметрам "
            "frozen-решения. Результаты используются только для выбора следующего "
            "backtest, не как causal production-оценка."
        ),
        nbformat.v4.new_markdown_cell("## Data"),
        nbformat.v4.new_code_cell(
            "from pathlib import Path\n"
            "import csv, json\n\n"
            "output_dir = Path('.')\n"
            "summary = json.loads((output_dir / 'analysis-summary.json').read_text(encoding='utf-8'))\n"
            "def read_csv(name):\n"
            "    with (output_dir / name).open(encoding='utf-8-sig') as handle:\n"
            "        return list(csv.DictReader(handle))\n"
            "candidates = read_csv('candidate-rules.csv')\n"
            "segments = read_csv('segment-effects.csv')\n"
            "sku_effects = read_csv('sku-effects.csv')\n"
            "len(sku_effects), len(candidates), len(segments)"
        ),
        nbformat.v4.new_markdown_cell("## Results"),
        nbformat.v4.new_code_cell(
            "assert summary['reconciliation']['replay_minus_persisted_sales'] == '0.00000'\n"
            "assert summary['reconciliation']['sku_sum_minus_replay_sales'] == '0.00000'\n"
            "summary['headline']"
        ),
        nbformat.v4.new_code_cell(
            "[row for row in candidates if float(row['economic_contribution_delta_rub']) > 0][:20]"
        ),
        nbformat.v4.new_code_cell(
            "[row for row in segments if row['segment_dimension'] == " "'demand_pattern_preperiod']"
        ),
        nbformat.v4.new_markdown_cell(
            "## Takeaways\n\n"
            "- Общий haircut остаётся отрицательным.\n"
            "- Положительный диагностический сегмент требует отдельного quick-v15.\n"
            "- Production и acceleration не затрагиваются."
        ),
    ]
    path = output_dir / "segment-analysis.ipynb"
    nbformat.write(notebook, path)
    client = NotebookClient(notebook, timeout=180, kernel_name="python3")
    client.execute(cwd=str(output_dir))
    nbformat.write(notebook, path)
    return path


def finalize_existing_analysis(output_dir: Path) -> dict[str, Any]:
    artifact_names = (
        "analysis-summary.json",
        "SEGMENT-DIAGNOSTIC.md",
        "sku-effects.csv",
        "segment-effects.csv",
        "candidate-rules.csv",
        "period-effects.csv",
        "segment-analysis.ipynb",
    )
    missing = [name for name in artifact_names if not (output_dir / name).is_file()]
    if missing:
        raise ValueError(f"analysis artifacts are missing: {', '.join(missing)}")
    manifest = {
        "schema": "display_auto_order_base_pipeline_segment_analysis_manifest.v1",
        "diagnostic_only": True,
        "production_authorized": False,
        "pdf_created": False,
        "files": {name: _sha256(output_dir / name) for name in artifact_names},
    }
    (output_dir / "analysis-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def build_analysis(
    *,
    preflight_dir: Path,
    quick_result_dir: Path,
    output_dir: Path,
    policy_json: Path,
    scenario_config_json: Path,
    challenger_role: str = "cautious",
) -> dict[str, Any]:
    if challenger_role not in {"hypothesis", "cautious"}:
        raise ValueError("challenger_role must be hypothesis or cautious")
    quick_summary = json.loads(
        (quick_result_dir / "frozen-summary.json").read_text(encoding="utf-8")
    )
    inputs = _prepare_inputs(preflight_dir)
    source_roles = quick_summary["source_scenario_roles"]
    selection = frozen.select_scenarios(
        inputs["frozen_scenarios"],
        run_mode=frozen.RUN_MODE_QUICK,
        control_scenario_id=source_roles["control"],
        hypothesis_scenario_id=source_roles["hypothesis"],
        cautious_scenario_id=source_roles["cautious"],
    )
    profiles = quick_summary["quick_base_pipeline"]
    selection = frozen.apply_quick_base_pipeline_profiles(
        selection,
        hypothesis_profile=profiles["hypothesis_profile"],
        cautious_profile=profiles["cautious_profile"],
    )
    by_id = {scenario.scenario_id: scenario for scenario in selection.scenarios}
    control_scenario = by_id[selection.scenario_roles["control"]]
    challenger_scenario = by_id[selection.scenario_roles[challenger_role]]
    shared_cache: dict[tuple[str, Any, int], list[Decimal]] = {}
    simulation_args = {
        "fact_rows_by_date": inputs["fact_rows_by_date"],
        "decision_rows_by_date": inputs["decision_rows_by_date"],
        "initial_pipeline_rows": inputs["initial_pipeline"],
        "sales_by_code": inputs["sales_by_code"],
        "policy": load_auto_order_policy(policy_json),
        "config": load_scenario_config(scenario_config_json),
        "date_from": inputs["date_from"],
        "date_to": inputs["date_to"],
        "keep_detail": True,
        "demand_sample_cache": shared_cache,
    }
    control = frozen.simulate_scenario(scenario=control_scenario, **simulation_args)
    challenger = frozen.simulate_scenario(scenario=challenger_scenario, **simulation_args)
    raw_sku_rows = _sku_rows(
        control=control,
        hypothesis=challenger,
        first_decision_by_code=inputs["first_decision_by_code"],
        sales_by_code=inputs["sales_by_code"],
        date_from=inputs["date_from"],
        date_to=inputs["date_to"],
    )
    sku_rows = enrich_sku_rows(raw_sku_rows)
    candidates = candidate_rule_rows(sku_rows)
    segments = segment_effect_rows(sku_rows)
    periods = _period_rows(
        control=control,
        hypothesis=challenger,
        date_from=inputs["date_from"],
        date_to=inputs["date_to"],
    )
    period_days = (inputs["date_to"] - inputs["date_from"]).days + 1
    control_summary = frozen._summary(
        scenario=control.scenario,
        strategy="model",
        metrics=control.model,
        period_days=period_days,
    )
    challenger_summary = frozen._summary(
        scenario=challenger.scenario,
        strategy="model",
        metrics=challenger.model,
        period_days=period_days,
    )
    persisted = next(
        row for row in quick_summary["quick_comparison"] if row["scenario_role"] == challenger_role
    )
    served_delta = _decimal(challenger_summary["served_observed_qty"]) - _decimal(
        control_summary["served_observed_qty"]
    )
    gross_profit_delta = _decimal(challenger_summary["gross_profit_rub"]) - _decimal(
        control_summary["gross_profit_rub"]
    )
    capital_delta = _decimal(challenger_summary["average_inventory_value_rub"]) - _decimal(
        control_summary["average_inventory_value_rub"]
    )
    economic_delta = _decimal(challenger_summary["economic_contribution_rub"]) - _decimal(
        control_summary["economic_contribution_rub"]
    )
    sku_sales_sum = sum(
        (_decimal(row.get("served_observed_delta_to_control_qty")) for row in sku_rows),
        ZERO,
    )
    summary: dict[str, Any] = {
        "schema": "display_auto_order_base_pipeline_segment_analysis.v1",
        "source_preflight_manifest_sha256": _sha256(preflight_dir / "run-manifest.json"),
        "source_quick_summary_sha256": _sha256(quick_result_dir / "frozen-summary.json"),
        "date_from": inputs["date_from"].isoformat(),
        "date_to": inputs["date_to"].isoformat(),
        "diagnostic_only": True,
        "production_authorized": False,
        "pdf_created": False,
        "scenario_ids": {
            "control": control.scenario.scenario_id,
            "challenger": challenger.scenario.scenario_id,
            "challenger_role": challenger_role,
        },
        "headline": {
            "served_observed_delta_qty": str(served_delta),
            "gross_profit_delta_rub": str(gross_profit_delta),
            "capital_delta_rub": str(capital_delta),
            "ending_inventory_delta_qty": str(
                _decimal(challenger_summary["ending_inventory_qty"])
                - _decimal(control_summary["ending_inventory_qty"])
            ),
            "economic_contribution_delta_rub": str(economic_delta),
            "sku_count": len(sku_rows),
            "affected_sku_count": sum(
                int(row.get("pipeline_profile_affected") or 0) for row in sku_rows
            ),
        },
        "top_candidate_rules": candidates[:30],
        "reconciliation": {
            "replay_minus_persisted_sales": str(
                served_delta - _decimal(persisted["served_observed_delta_to_control_qty"])
            ),
            "replay_minus_persisted_economic": str(
                economic_delta - _decimal(persisted["economic_contribution_delta_to_control_rub"])
            ),
            "sku_sum_minus_replay_sales": str(sku_sales_sum - served_delta),
        },
        "method": {
            "decision_grain": "SKU rule composed from pre-period demand pattern and start-of-period cost, margin, stage, confidence, and P75",
            "candidate_grid": "480 predefined combinations; diagnostic ranking only; selected rule must pass a separate dynamic quick backtest",
            "selection_bias_guard": "no individual SKU allowlist and no future outcome field in the candidate predicate",
            "known_limit": "start-of-period economics approximate current-decision economics; quick-v15 validates the dynamic rule",
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(output_dir / "sku-effects.csv", sku_rows)
    _write_csv(output_dir / "segment-effects.csv", segments)
    _write_csv(output_dir / "candidate-rules.csv", candidates)
    _write_csv(output_dir / "period-effects.csv", periods)
    (output_dir / "analysis-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output_dir / "SEGMENT-DIAGNOSTIC.md").write_text(_build_markdown(summary), encoding="utf-8")
    notebook_path = _build_notebook(output_dir, summary)
    assert notebook_path.name == "segment-analysis.ipynb"
    finalize_existing_analysis(output_dir)
    return summary


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight-dir", type=Path)
    parser.add_argument("--quick-result-dir", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--challenger-role",
        choices=("hypothesis", "cautious"),
        default="cautious",
    )
    parser.add_argument(
        "--finalize-existing",
        action="store_true",
        help="Write the checksum manifest for an already completed analysis directory",
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
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.finalize_existing:
        manifest = finalize_existing_analysis(args.output_dir)
        print(json.dumps(manifest, ensure_ascii=False))
        return 0
    if args.preflight_dir is None or args.quick_result_dir is None:
        raise SystemExit("--preflight-dir and --quick-result-dir are required")
    summary = build_analysis(
        preflight_dir=args.preflight_dir,
        quick_result_dir=args.quick_result_dir,
        output_dir=args.output_dir,
        policy_json=args.auto_order_policy_json,
        scenario_config_json=args.scenario_config_json,
        challenger_role=args.challenger_role,
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
