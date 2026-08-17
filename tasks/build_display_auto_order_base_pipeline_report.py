"""Build the stakeholder report for the segmented base-pipeline quick backtest.

The task reads only frozen quick/replay artifacts, writes the canonical Data
Analytics ``artifact.json`` and supporting analysis files, and never creates
purchase orders or accesses production sources.
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

ZERO = Decimal("0")
STAGE_LABELS = {
    "new_item": "Новинка",
    "sales_start": "Пошли продажи",
    "sale": "Растим",
    "working": "Поддерживаем",
}
STAGE_ORDER = {name: index for index, name in enumerate(STAGE_LABELS)}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(_clean(value) or "0")
    except (ArithmeticError, ValueError):
        return ZERO


def _number(value: Any) -> float:
    return float(_decimal(value))


def _number_ru(value: Any, digits: int = 1) -> str:
    return f"{_number(value):,.{digits}f}".replace(",", " ").replace(".", ",")


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _scenario(summary: Mapping[str, Any], role: str) -> Mapping[str, Any]:
    return next(row for row in summary["quick_comparison"] if row["scenario_role"] == role)


def _model_summary_row(directory: Path, scenario_id: str) -> Mapping[str, Any]:
    return next(
        row
        for row in _read_csv(directory / "frozen-scenario-summary.csv")
        if row["scenario_id"] == scenario_id and row["strategy"] == "model"
    )


def _relative_source_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(Path.cwd().resolve()).as_posix()
    except ValueError as exc:
        raise ValueError(f"report source must be inside the repository: {path}") from exc


def economic_concentration(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    affected = [row for row in rows if int(row.get("pipeline_profile_affected") or 0)]
    positive = sorted(
        (
            max(ZERO, _decimal(row.get("economic_contribution_delta_to_control_rub")))
            for row in affected
        ),
        reverse=True,
    )
    negative = [
        min(ZERO, _decimal(row.get("economic_contribution_delta_to_control_rub")))
        for row in affected
    ]
    positive_total = sum(positive, ZERO)

    def share(top_n: int) -> Decimal:
        return sum(positive[:top_n], ZERO) / positive_total if positive_total > ZERO else ZERO

    return {
        "affected_sku_count": len(affected),
        "sales_gain_sku_count": sum(
            _decimal(row.get("served_observed_delta_to_control_qty")) > ZERO for row in affected
        ),
        "positive_economic_sku_count": sum(
            _decimal(row.get("economic_contribution_delta_to_control_rub")) > ZERO
            for row in affected
        ),
        "negative_economic_sku_count": sum(
            _decimal(row.get("economic_contribution_delta_to_control_rub")) < ZERO
            for row in affected
        ),
        "gross_positive_economic_rub": str(positive_total),
        "gross_negative_economic_rub": str(sum(negative, ZERO)),
        "top_1_positive_share": str(share(1)),
        "top_5_positive_share": str(share(5)),
        "top_10_positive_share": str(share(10)),
    }


def build_analysis(
    *,
    v14_dir: Path,
    v15_dir: Path,
    replay_dir: Path,
) -> dict[str, Any]:
    v14 = json.loads((v14_dir / "frozen-summary.json").read_text(encoding="utf-8"))
    v15 = json.loads((v15_dir / "frozen-summary.json").read_text(encoding="utf-8"))
    replay = json.loads((replay_dir / "analysis-summary.json").read_text(encoding="utf-8"))
    if any(_decimal(value) != ZERO for value in replay["reconciliation"].values()):
        raise ValueError("quick-v15 replay does not reconcile to persisted metrics")

    v14_blanket = _scenario(v14, "cautious")
    v15_main = _scenario(v15, "hypothesis")
    v15_cautious = _scenario(v15, "cautious")
    control = _scenario(v15, "control")
    actual = v15["base_actual"]
    control_model = v15["control_model"]
    main_model = v15["base_model"]
    sku_rows = _read_csv(replay_dir / "sku-effects.csv")
    concentration = economic_concentration(sku_rows)

    segment_rows = _read_csv(replay_dir / "segment-effects.csv")
    stage_rows = [
        {
            "stage_code": row["segment_value"],
            "stage_label": STAGE_LABELS[row["segment_value"]],
            "affected_sku_count": int(row["affected_sku_count"]),
            "sales_delta_qty": _number(row["served_observed_delta_qty"]),
            "gross_profit_delta_rub": _number(row["gross_profit_delta_rub"]),
            "capital_delta_rub": _number(row["capital_delta_rub"]),
            "economic_delta_rub": _number(row["economic_contribution_delta_rub"]),
        }
        for row in segment_rows
        if row["segment_dimension"] == "stage_at_period_start"
        and row["segment_value"] in STAGE_LABELS
    ]
    stage_rows.sort(key=lambda row: STAGE_ORDER[str(row["stage_code"])])

    pattern_rows = [
        {
            "pattern": row["segment_value"],
            "affected_sku_count": int(row["affected_sku_count"]),
            "sales_delta_qty": _number(row["served_observed_delta_qty"]),
            "economic_delta_rub": _number(row["economic_contribution_delta_rub"]),
        }
        for row in segment_rows
        if row["segment_dimension"] == "demand_pattern_preperiod"
        and int(row["affected_sku_count"]) > 0
    ]
    pattern_rows.sort(key=lambda row: -float(row["economic_delta_rub"]))

    period_rows = [
        {
            "period": row["period"],
            "period_label": "Февраль–июнь" if row["period"] == "pre_july" else "Июль",
            "sales_delta_qty": _number(row["served_observed_qty"]),
            "gross_profit_delta_rub": _number(row["gross_profit_rub"]),
            "capital_delta_rub": _number(row["average_inventory_value_rub"]),
            "economic_delta_rub": _number(row["economic_contribution_rub"]),
        }
        for row in _read_csv(replay_dir / "period-effects.csv")
        if row["scenario_role"] == "hypothesis_minus_control"
    ]

    control_gap_qty = _decimal(actual["served_observed_qty"]) - _decimal(
        control["served_observed_qty"]
    )
    main_sales_delta = _decimal(v15_main["served_observed_delta_to_control_qty"])
    strict_sales_gap = _decimal(actual["served_observed_qty"]) - _decimal(
        v15_main["served_observed_qty"]
    )
    strict_profit_gap = _decimal(actual["gross_profit_rub"]) - _decimal(
        v15_main["gross_profit_rub"]
    )
    strict_capital_advantage = _decimal(actual["average_inventory_value_rub"]) - _decimal(
        v15_main["average_inventory_value_rub"]
    )

    scenario_rows = [
        {
            "scenario_key": "v14_blanket_95",
            "chart_label": "v14: общий 95%",
            "scenario_label": "v14: 95% всем medium + 75% low",
            "sales_delta_qty": _number(v14_blanket["served_observed_delta_to_control_qty"]),
            "gross_profit_delta_rub": _number(v14_blanket["gross_profit_delta_to_control_rub"]),
            "capital_delta_rub": _number(v14_blanket["capital_delta_to_control_rub"]),
            "ending_inventory_delta_qty": _number(
                v14_blanket["ending_inventory_delta_to_control_qty"]
            ),
            "economic_delta_rub": _number(
                v14_blanket["economic_contribution_delta_to_control_rub"]
            ),
            "fill_rate_delta_pp": _number(v14_blanket["observed_fill_rate_delta_to_control"]) * 100,
        },
        {
            "scenario_key": "v15_ratio_050",
            "chart_label": "v15: порог ≥0,5",
            "scenario_label": "v15: 95% при марже/себестоимости ≥ 0,5",
            "sales_delta_qty": _number(v15_main["served_observed_delta_to_control_qty"]),
            "gross_profit_delta_rub": _number(v15_main["gross_profit_delta_to_control_rub"]),
            "capital_delta_rub": _number(v15_main["capital_delta_to_control_rub"]),
            "ending_inventory_delta_qty": _number(
                v15_main["ending_inventory_delta_to_control_qty"]
            ),
            "economic_delta_rub": _number(v15_main["economic_contribution_delta_to_control_rub"]),
            "fill_rate_delta_pp": _number(v15_main["observed_fill_rate_delta_to_control"]) * 100,
        },
        {
            "scenario_key": "v15_ratio_100",
            "chart_label": "v15: порог ≥1,0",
            "scenario_label": "v15: 95% при марже/себестоимости ≥ 1,0",
            "sales_delta_qty": _number(v15_cautious["served_observed_delta_to_control_qty"]),
            "gross_profit_delta_rub": _number(v15_cautious["gross_profit_delta_to_control_rub"]),
            "capital_delta_rub": _number(v15_cautious["capital_delta_to_control_rub"]),
            "ending_inventory_delta_qty": _number(
                v15_cautious["ending_inventory_delta_to_control_qty"]
            ),
            "economic_delta_rub": _number(
                v15_cautious["economic_contribution_delta_to_control_rub"]
            ),
            "fill_rate_delta_pp": _number(v15_cautious["observed_fill_rate_delta_to_control"])
            * 100,
        },
    ]
    acceptance_rows = [
        {
            "scenario": "Факт",
            "served_observed_qty": _number(actual["served_observed_qty"]),
            "observed_fill_rate": _number(actual["observed_fill_rate"]),
            "gross_profit_rub": _number(actual["gross_profit_rub"]),
            "average_inventory_value_rub": _number(actual["average_inventory_value_rub"]),
            "gmroi_annualized": _number(actual["gmroi_annualized"]),
            "strict_result": "База сравнения",
        },
        {
            "scenario": "Контроль модели",
            "served_observed_qty": _number(control_model["served_observed_qty"]),
            "observed_fill_rate": _number(control_model["observed_fill_rate"]),
            "gross_profit_rub": _number(control_model["gross_profit_rub"]),
            "average_inventory_value_rub": _number(control_model["average_inventory_value_rub"]),
            "gmroi_annualized": _number(control_model["gmroi_annualized"]),
            "strict_result": "Не прошёл",
        },
        {
            "scenario": "v15, порог 0,5",
            "served_observed_qty": _number(main_model["served_observed_qty"]),
            "observed_fill_rate": _number(main_model["observed_fill_rate"]),
            "gross_profit_rub": _number(main_model["gross_profit_rub"]),
            "average_inventory_value_rub": _number(main_model["average_inventory_value_rub"]),
            "gmroi_annualized": _number(main_model["gmroi_annualized"]),
            "strict_result": "Не прошёл",
        },
    ]
    generated_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return {
        "schema": "display_auto_order_base_pipeline_report.v1",
        "generated_at": generated_at,
        "date_from": v15["date_from"],
        "date_to": v15["date_to"],
        "scenario_rows": scenario_rows,
        "period_rows": period_rows,
        "stage_rows": stage_rows,
        "pattern_rows": pattern_rows,
        "acceptance_rows": acceptance_rows,
        "concentration": concentration,
        "headline": {
            "sales_delta_qty": str(main_sales_delta),
            "gross_profit_delta_rub": v15_main["gross_profit_delta_to_control_rub"],
            "capital_delta_rub": v15_main["capital_delta_to_control_rub"],
            "ending_inventory_delta_qty": v15_main["ending_inventory_delta_to_control_qty"],
            "economic_delta_rub": v15_main["economic_contribution_delta_to_control_rub"],
            "control_gap_qty": str(control_gap_qty),
            "control_gap_recovered_share": str(
                main_sales_delta / control_gap_qty if control_gap_qty > ZERO else ZERO
            ),
            "strict_sales_gap_qty": str(strict_sales_gap),
            "strict_profit_gap_rub": str(strict_profit_gap),
            "strict_capital_advantage_rub": str(strict_capital_advantage),
            "strict_acceptance_passed": bool(v15["acceptance"]["passed"]),
            "affected_sku_count": replay["headline"]["affected_sku_count"],
        },
        "reconciliation": replay["reconciliation"],
        "production_authorized": False,
        "pdf_created": False,
    }


def build_lot_risk_analysis(
    *,
    v15_dir: Path,
    v16_dir: Path,
    v15_report_dir: Path,
    replay_dir: Path,
    report_analysis_path: Path,
) -> dict[str, Any]:
    v15 = json.loads((v15_dir / "frozen-summary.json").read_text(encoding="utf-8"))
    v16 = json.loads((v16_dir / "frozen-summary.json").read_text(encoding="utf-8"))
    prior_report = json.loads((v15_report_dir / "report-analysis.json").read_text(encoding="utf-8"))
    replay = json.loads((replay_dir / "analysis-summary.json").read_text(encoding="utf-8"))
    if any(_decimal(value) != ZERO for value in replay["reconciliation"].values()):
        raise ValueError("quick-v16 replay does not reconcile to persisted metrics")

    control = _scenario(v16, "control")
    v15_main = _scenario(v15, "hypothesis")
    p50 = _scenario(v16, "hypothesis")
    p75 = _scenario(v16, "cautious")
    actual = v16["base_actual"]
    control_model = v16["control_model"]
    v15_model = _model_summary_row(v15_dir, _clean(v15["scenario_roles"]["hypothesis"]))
    p75_model = _model_summary_row(v16_dir, _clean(v16["scenario_roles"]["cautious"]))

    def scenario_row(
        *, key: str, chart_label: str, label: str, row: Mapping[str, Any]
    ) -> dict[str, Any]:
        return {
            "scenario_key": key,
            "chart_label": chart_label,
            "scenario_label": label,
            "sales_delta_qty": _number(row["served_observed_delta_to_control_qty"]),
            "gross_profit_delta_rub": _number(row["gross_profit_delta_to_control_rub"]),
            "capital_delta_rub": _number(row["capital_delta_to_control_rub"]),
            "ending_inventory_delta_qty": _number(row["ending_inventory_delta_to_control_qty"]),
            "economic_delta_rub": _number(row["economic_contribution_delta_to_control_rub"]),
        }

    scenario_rows = [
        scenario_row(
            key="v15_dynamic_margin",
            chart_label="v15: без риска партии",
            label="v15: medium 95% при марже/себестоимости ≥ 0,5",
            row=v15_main,
        ),
        scenario_row(
            key="v16_lot_risk_p50",
            chart_label="v16: граница P50",
            label="v16: обычная партия 95%, партия дальше P50 — 90%",
            row=p50,
        ),
        scenario_row(
            key="v16_lot_risk_p75",
            chart_label="v16: граница P75",
            label="v16: обычная партия 95%, партия дальше P75 — 90%",
            row=p75,
        ),
    ]

    v15_by_period = {row["period"]: row for row in prior_report["period_rows"]}
    period_rows = []
    for row in _read_csv(replay_dir / "period-effects.csv"):
        if row["scenario_role"] != "hypothesis_minus_control":
            continue
        prior = v15_by_period[row["period"]]
        period_rows.append(
            {
                "period": row["period"],
                "period_label": ("Февраль–июнь" if row["period"] == "pre_july" else "Июль"),
                "sales_delta_to_control_qty": _number(row["served_observed_qty"]),
                "economic_delta_to_control_rub": _number(row["economic_contribution_rub"]),
                "incremental_sales_vs_v15_qty": (
                    _number(row["served_observed_qty"]) - _number(prior["sales_delta_qty"])
                ),
                "incremental_gross_profit_vs_v15_rub": (
                    _number(row["gross_profit_rub"]) - _number(prior["gross_profit_delta_rub"])
                ),
                "incremental_capital_vs_v15_rub": (
                    _number(row["average_inventory_value_rub"])
                    - _number(prior["capital_delta_rub"])
                ),
                "incremental_economic_vs_v15_rub": (
                    _number(row["economic_contribution_rub"]) - _number(prior["economic_delta_rub"])
                ),
            }
        )

    v15_by_stage = {row["stage_code"]: row for row in prior_report["stage_rows"]}
    stage_rows = []
    for row in _read_csv(replay_dir / "segment-effects.csv"):
        if (
            row["segment_dimension"] != "stage_at_period_start"
            or row["segment_value"] not in STAGE_LABELS
        ):
            continue
        prior = v15_by_stage[row["segment_value"]]
        stage_rows.append(
            {
                "stage_code": row["segment_value"],
                "stage_label": STAGE_LABELS[row["segment_value"]],
                "affected_sku_count": int(row["affected_sku_count"]),
                "sales_delta_to_control_qty": _number(row["served_observed_delta_qty"]),
                "economic_delta_to_control_rub": _number(row["economic_contribution_delta_rub"]),
                "incremental_sales_vs_v15_qty": (
                    _number(row["served_observed_delta_qty"]) - _number(prior["sales_delta_qty"])
                ),
                "incremental_economic_vs_v15_rub": (
                    _number(row["economic_contribution_delta_rub"])
                    - _number(prior["economic_delta_rub"])
                ),
            }
        )
    stage_rows.sort(key=lambda row: STAGE_ORDER[str(row["stage_code"])])

    p75_sales_delta = _decimal(p75["served_observed_delta_to_control_qty"])
    control_gap_qty = _decimal(actual["served_observed_qty"]) - _decimal(
        control["served_observed_qty"]
    )
    headline = {
        "sales_delta_to_control_qty": str(p75_sales_delta),
        "incremental_sales_vs_v15_qty": str(
            p75_sales_delta - _decimal(v15_main["served_observed_delta_to_control_qty"])
        ),
        "gross_profit_delta_to_control_rub": p75["gross_profit_delta_to_control_rub"],
        "incremental_gross_profit_vs_v15_rub": str(
            _decimal(p75["gross_profit_delta_to_control_rub"])
            - _decimal(v15_main["gross_profit_delta_to_control_rub"])
        ),
        "capital_delta_to_control_rub": p75["capital_delta_to_control_rub"],
        "incremental_capital_vs_v15_rub": str(
            _decimal(p75["capital_delta_to_control_rub"])
            - _decimal(v15_main["capital_delta_to_control_rub"])
        ),
        "economic_delta_to_control_rub": p75["economic_contribution_delta_to_control_rub"],
        "incremental_economic_vs_v15_rub": str(
            _decimal(p75["economic_contribution_delta_to_control_rub"])
            - _decimal(v15_main["economic_contribution_delta_to_control_rub"])
        ),
        "ending_inventory_delta_to_control_qty": p75["ending_inventory_delta_to_control_qty"],
        "strict_sales_gap_qty": str(
            _decimal(actual["served_observed_qty"]) - _decimal(p75["served_observed_qty"])
        ),
        "strict_profit_gap_rub": str(
            _decimal(actual["gross_profit_rub"]) - _decimal(p75["gross_profit_rub"])
        ),
        "strict_capital_advantage_rub": str(
            _decimal(actual["average_inventory_value_rub"])
            - _decimal(p75["average_inventory_value_rub"])
        ),
        "control_gap_recovered_share": str(
            p75_sales_delta / control_gap_qty if control_gap_qty > ZERO else ZERO
        ),
        "affected_sku_count": replay["headline"]["affected_sku_count"],
        "strict_acceptance_passed": bool(p75["acceptance_passed"]),
    }
    lot_risk_rows = [
        {
            "profile": "P50",
            "boundary": "Остаточный срок строго больше текущего P50",
            "positive_evaluations": int(p50["base_pipeline_lot_risk_positive_evaluations"]),
            "risky_pipeline_qty": _number(p50["base_pipeline_lot_risk_qty_evaluated"]),
            "extra_pipeline_reduction_qty": _number(
                p50["base_pipeline_lot_risk_effective_reduction_qty"]
            ),
            "incremental_sales_vs_v15_qty": _number(p50["served_observed_delta_to_control_qty"])
            - _number(v15_main["served_observed_delta_to_control_qty"]),
            "incremental_economic_vs_v15_rub": _number(
                p50["economic_contribution_delta_to_control_rub"]
            )
            - _number(v15_main["economic_contribution_delta_to_control_rub"]),
        },
        {
            "profile": "P75",
            "boundary": "Остаточный срок строго больше текущего P75",
            "positive_evaluations": int(p75["base_pipeline_lot_risk_positive_evaluations"]),
            "risky_pipeline_qty": _number(p75["base_pipeline_lot_risk_qty_evaluated"]),
            "extra_pipeline_reduction_qty": _number(
                p75["base_pipeline_lot_risk_effective_reduction_qty"]
            ),
            "incremental_sales_vs_v15_qty": _number(p75["served_observed_delta_to_control_qty"])
            - _number(v15_main["served_observed_delta_to_control_qty"]),
            "incremental_economic_vs_v15_rub": _number(
                p75["economic_contribution_delta_to_control_rub"]
            )
            - _number(v15_main["economic_contribution_delta_to_control_rub"]),
        },
    ]
    acceptance_rows = [
        {
            "scenario": "Факт",
            "served_observed_qty": _number(actual["served_observed_qty"]),
            "observed_fill_rate": _number(actual["observed_fill_rate"]),
            "gross_profit_rub": _number(actual["gross_profit_rub"]),
            "average_inventory_value_rub": _number(actual["average_inventory_value_rub"]),
            "gmroi_annualized": _number(actual["gmroi_annualized"]),
            "strict_result": "База сравнения",
        },
        {
            "scenario": "Контроль модели",
            "served_observed_qty": _number(control_model["served_observed_qty"]),
            "observed_fill_rate": _number(control_model["observed_fill_rate"]),
            "gross_profit_rub": _number(control_model["gross_profit_rub"]),
            "average_inventory_value_rub": _number(control_model["average_inventory_value_rub"]),
            "gmroi_annualized": _number(control_model["gmroi_annualized"]),
            "strict_result": "Не пройден",
        },
        {
            "scenario": "v15",
            "served_observed_qty": _number(v15_model["served_observed_qty"]),
            "observed_fill_rate": _number(v15_model["observed_fill_rate"]),
            "gross_profit_rub": _number(v15_model["gross_profit_rub"]),
            "average_inventory_value_rub": _number(v15_model["average_inventory_value_rub"]),
            "gmroi_annualized": _number(v15_model["gmroi_annualized"]),
            "strict_result": "Не пройден",
        },
        {
            "scenario": "v16 P75",
            "served_observed_qty": _number(p75_model["served_observed_qty"]),
            "observed_fill_rate": _number(p75_model["observed_fill_rate"]),
            "gross_profit_rub": _number(p75_model["gross_profit_rub"]),
            "average_inventory_value_rub": _number(p75_model["average_inventory_value_rub"]),
            "gmroi_annualized": _number(p75_model["gmroi_annualized"]),
            "strict_result": "Не пройден",
        },
    ]
    return {
        "schema": "display_auto_order_lot_risk_report_analysis.v1",
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "date_from": v16["date_from"],
        "date_to": v16["date_to"],
        "headline": headline,
        "scenario_rows": scenario_rows,
        "lot_risk_rows": lot_risk_rows,
        "period_rows": period_rows,
        "stage_rows": stage_rows,
        "acceptance_rows": acceptance_rows,
        "reconciliation": replay["reconciliation"],
        "source_paths": {
            "v15_summary": _relative_source_path(v15_dir / "frozen-summary.json"),
            "v16_summary": _relative_source_path(v16_dir / "frozen-summary.json"),
            "v16_replay": _relative_source_path(replay_dir / "analysis-summary.json"),
            "v16_segments": _relative_source_path(replay_dir / "segment-effects.csv"),
            "report_analysis": _relative_source_path(report_analysis_path),
            "method_policy": "docs/specs/assortment-lifecycle-policy.md",
        },
    }


def _source(
    source_id: str,
    label: str,
    path: str,
    sql: str,
    description: str,
) -> dict[str, Any]:
    return {
        "id": source_id,
        "label": label,
        "path": path,
        "query": {
            "engine": "DuckDB SQL over frozen CSV/JSON artifacts",
            "language": "sql",
            "sql": sql,
            "description": description,
            "filters": [
                "Период 2026-02-01 — 2026-07-31",
                "Все 2 605 SKU предмета Дисплеи",
                "Только frozen-файлы; production-запросы отсутствуют",
            ],
            "metric_definitions": [
                "Экономический вклад = валовая прибыль минус стоимость содержания среднего запаса за период.",
                "Средний капитал = средняя дневная стоимость модельного остатка.",
                "Fill rate = обслуженные записанные продажи / записанный спрос.",
            ],
            "tables_used": [path],
        },
    }


def build_artifact(analysis: Mapping[str, Any]) -> dict[str, Any]:
    headline = analysis["headline"]
    title = "Автозаказ дисплеев: сегментация дала плюс, но цель ещё не достигнута"
    report_root = "reports/assortment_lifecycle/backtest-2026-02-01_2026-07-31"
    v15_path = (
        f"{report_root}/next-stage-model-quick-segmented-base-pipeline-v15/frozen-summary.json"
    )
    v14_path = f"{report_root}/next-stage-model-quick-base-pipeline-v14/frozen-summary.json"
    replay_path = (
        f"{report_root}/next-stage-model-segmented-base-pipeline-analysis-post-v15/"
        "analysis-summary.json"
    )
    segment_path = (
        f"{report_root}/next-stage-model-segmented-base-pipeline-analysis-post-v15/"
        "segment-effects.csv"
    )
    report_analysis_path = (
        f"{report_root}/next-stage-model-segmented-base-pipeline-report-v15/" "report-analysis.json"
    )
    sources = [
        _source(
            "v15_summary",
            "Frozen quick-v15: итог сценариев",
            v15_path,
            f"SELECT * FROM read_json_auto('{v15_path}');",
            "Сравнивает контроль, динамический порог 0,5 и чувствительность 1,0.",
        ),
        _source(
            "v14_summary",
            "Frozen quick-v14: общий haircut pipeline",
            v14_path,
            f"SELECT * FROM read_json_auto('{v14_path}');",
            "Даёт контрольную отрицательную экономику общего правила 95%.",
        ),
        _source(
            "v15_replay",
            "Независимый SKU-replay quick-v15",
            replay_path,
            f"SELECT * FROM read_json_auto('{replay_path}');",
            "Повторно считает контроль и v15 с детализацией по SKU и периодам.",
        ),
        _source(
            "v15_segments",
            "Стадии и сегменты SKU-replay",
            segment_path,
            f"SELECT * FROM read_csv_auto('{segment_path}', header=true);",
            "Агрегирует динамический эффект v15 по стадии на начало периода и типу спроса.",
        ),
        _source(
            "report_analysis",
            "Сверенное сравнение quick-v14 и quick-v15",
            report_analysis_path,
            f"SELECT * FROM read_json_auto('{report_analysis_path}');",
            "Объединяет сверенные frozen-метрики v14, v15, периодов, стадий и концентрации.",
        ),
        _source(
            "method_policy",
            "Каноническая методика жизненного цикла ассортимента",
            "docs/specs/assortment-lifecycle-policy.md",
            "SELECT content FROM read_text('docs/specs/assortment-lifecycle-policy.md');",
            "Фиксирует стадии, frozen-методику, критерий успеха и запрет production.",
        ),
    ]
    cards = [
        {
            "id": "economic_card",
            "description": "Чистый эффект v15 после стоимости содержания дополнительного запаса.",
            "dataset": "headline",
            "sourceId": "v15_summary",
            "metrics": [
                {
                    "label": "Δ экономический вклад",
                    "field": "economic_delta_rub",
                    "format": "currency",
                    "signed": True,
                }
            ],
        },
        {
            "id": "sales_card",
            "description": "Дополнительные записанные продажи относительно контроля модели.",
            "dataset": "headline",
            "sourceId": "v15_summary",
            "metrics": [
                {
                    "label": "Дополнительные продажи",
                    "field": "sales_delta_qty",
                    "format": "number",
                    "signed": True,
                },
                {
                    "label": "доля закрытого разрыва",
                    "field": "control_gap_recovered_share",
                    "format": "percent",
                },
            ],
        },
        {
            "id": "capital_card",
            "description": "Дополнительный средний складской капитал к контролю.",
            "dataset": "headline",
            "sourceId": "v15_summary",
            "metrics": [
                {
                    "label": "Δ средний капитал",
                    "field": "capital_delta_rub",
                    "format": "currency",
                    "signed": True,
                }
            ],
        },
    ]
    charts = [
        {
            "id": "profile_economics_chart",
            "title": "Экономический вклад вариантов pipeline",
            "subtitle": "Относительно одного frozen-контроля, февраль–июль 2026 года, ₽.",
            "showDescription": True,
            "type": "bar",
            "dataset": "scenario_comparison",
            "sourceId": "report_analysis",
            "encodings": {
                "x": {"field": "chart_label", "type": "nominal", "label": "Правило"},
                "y": {
                    "field": "economic_delta_rub",
                    "type": "quantitative",
                    "label": "Δ экономический вклад, ₽",
                },
                "tooltip": [
                    {
                        "field": "sales_delta_qty",
                        "type": "quantitative",
                        "label": "Δ продажи, шт.",
                    },
                    {
                        "field": "capital_delta_rub",
                        "type": "quantitative",
                        "label": "Δ средний капитал, ₽",
                        "format": "currency",
                    },
                ],
            },
            "valueFormat": "currency",
            "layout": "full",
            "palette": {"kind": "categorical"},
            "referenceLines": [{"axis": "y", "value": 0, "label": "Ноль", "style": "dashed"}],
        },
        {
            "id": "period_economics_chart",
            "title": "Экономический вклад v15 по периодам",
            "subtitle": "Февраль–июнь и июль показаны отдельно, ₽ к контролю.",
            "showDescription": True,
            "type": "bar",
            "dataset": "period_comparison",
            "sourceId": "v15_replay",
            "encodings": {
                "x": {"field": "period_label", "type": "ordinal", "label": "Период"},
                "y": {
                    "field": "economic_delta_rub",
                    "type": "quantitative",
                    "label": "Δ экономический вклад, ₽",
                },
                "tooltip": [
                    {
                        "field": "sales_delta_qty",
                        "type": "quantitative",
                        "label": "Δ продажи, шт.",
                    },
                    {
                        "field": "capital_delta_rub",
                        "type": "quantitative",
                        "label": "Δ средний капитал, ₽",
                        "format": "currency",
                    },
                ],
            },
            "valueFormat": "currency",
            "layout": "full",
            "palette": {"kind": "categorical"},
        },
        {
            "id": "stage_economics_chart",
            "title": "Экономический вклад v15 по стадиям",
            "subtitle": "Стадия SKU на начало frozen-периода, ₽ к контролю.",
            "showDescription": True,
            "type": "bar",
            "dataset": "stage_comparison",
            "sourceId": "v15_segments",
            "encodings": {
                "x": {"field": "stage_label", "type": "ordinal", "label": "Стадия"},
                "y": {
                    "field": "economic_delta_rub",
                    "type": "quantitative",
                    "label": "Δ экономический вклад, ₽",
                },
                "tooltip": [
                    {
                        "field": "sales_delta_qty",
                        "type": "quantitative",
                        "label": "Δ продажи, шт.",
                    },
                    {
                        "field": "affected_sku_count",
                        "type": "quantitative",
                        "label": "Затронуто SKU",
                    },
                ],
            },
            "valueFormat": "currency",
            "layout": "full",
            "palette": {"kind": "categorical"},
        },
    ]
    tables = [
        {
            "id": "scenario_table",
            "title": "Точное сравнение правил",
            "subtitle": "Изменения относительно frozen-контроля за февраль–июль 2026 года.",
            "dataset": "scenario_comparison",
            "sourceId": "report_analysis",
            "defaultSort": {"field": "economic_delta_rub", "direction": "desc"},
            "columns": [
                {"field": "scenario_label", "label": "Правило", "type": "text"},
                {
                    "field": "sales_delta_qty",
                    "label": "Δ продажи, шт.",
                    "format": "number",
                    "movement": True,
                },
                {
                    "field": "gross_profit_delta_rub",
                    "label": "Δ валовая прибыль",
                    "format": "currency",
                    "movement": True,
                },
                {
                    "field": "capital_delta_rub",
                    "label": "Δ средний капитал",
                    "format": "currency",
                    "movement": True,
                },
                {
                    "field": "ending_inventory_delta_qty",
                    "label": "Δ остаток, шт.",
                    "format": "number",
                    "movement": True,
                },
                {
                    "field": "economic_delta_rub",
                    "label": "Δ экономический вклад",
                    "format": "currency",
                    "movement": True,
                },
            ],
        },
        {
            "id": "acceptance_table",
            "title": "Проверка строгого критерия",
            "subtitle": "Факт, контроль и основной v15 на одном frozen-периоде.",
            "dataset": "acceptance_comparison",
            "sourceId": "v15_summary",
            "defaultSort": {"field": "served_observed_qty", "direction": "desc"},
            "columns": [
                {"field": "scenario", "label": "Вариант", "type": "text"},
                {
                    "field": "served_observed_qty",
                    "label": "Продажи, шт.",
                    "format": "number",
                },
                {
                    "field": "observed_fill_rate",
                    "label": "Fill rate",
                    "format": "percent",
                },
                {
                    "field": "gross_profit_rub",
                    "label": "Валовая прибыль",
                    "format": "currency",
                },
                {
                    "field": "average_inventory_value_rub",
                    "label": "Средний капитал",
                    "format": "currency",
                },
                {
                    "field": "gmroi_annualized",
                    "label": "GMROI годовой",
                    "format": "number",
                },
                {"field": "strict_result", "label": "Строгий итог", "type": "text"},
            ],
        },
    ]

    blocks = [
        {"id": "title", "type": "markdown", "body": f"# {title}"},
        {
            "id": "executive_summary",
            "type": "markdown",
            "body": (
                "## Executive Summary\n\n"
                f"- **Узкая сегментация исправила экономику гипотезы.** Порог `маржа/себестоимость ≥ 0,5` вернул **+{_number_ru(headline['sales_delta_qty'], 0)} продажи**, добавил **+146,9 тыс. ₽ валовой прибыли** и дал **+83,1 тыс. ₽ экономического вклада** при росте среднего капитала на **197,8 тыс. ₽** к контролю.\n"
                "- **Общий haircut был ошибкой.** v14 применял недоверие слишком широко и дал −163,7 тыс. ₽; v15 оставляет high- и low-confidence pipeline целиком и снижает только medium-confidence для экономически подходящего SKU.\n"
                f"- **Главная цель пока не достигнута.** Строгий критерий не пройден: до факта всё ещё не хватает **{_number_ru(headline['strict_sales_gap_qty'], 2)} продажи** и **9,58 млн ₽ валовой прибыли**. Production и создание заказов остаются заблокированными."
            ),
        },
        {
            "id": "headline_metrics",
            "type": "metric-strip",
            "cardIds": ["economic_card", "sales_card", "capital_card"],
        },
        {
            "id": "scope",
            "type": "markdown",
            "sourceId": "method_policy",
            "body": (
                "## Что именно проверено\n\n"
                "Сравнение проведено на всех 2 605 SKU дисплеев за 1 февраля–31 июля 2026 года. Историческая стадия на каждую дату восстановлена только по прошлым продажам. Сайт, КМП4, резерв, min/max и недельный пересмотр не менялись. **Единственное новое правило:** при средней уверенности в сроке поставки считать pipeline на 95% только если текущая валовая маржа на единицу составляет не менее половины текущей себестоимости; при нулевой или неизвестной себестоимости правило не включается."
            ),
        },
        {
            "id": "profile_finding",
            "type": "markdown",
            "body": (
                "## Адресный допуск лучше общего недоверия к поставкам\n\n"
                "v15 не ограничивает количество заказа рублёвым cap. Он только разрешает небольшой страховой заказ там, где возможная продажа экономически оправдывает риск лишнего запаса. Порог 0,5 дал лучший баланс; порог 1,0 оказался безопаснее, но почти не улучшил сервис."
            ),
        },
        {"id": "profile_chart", "type": "chart", "chartId": "profile_economics_chart"},
        {"id": "profile_table_block", "type": "table", "tableId": "scenario_table"},
        {
            "id": "period_finding",
            "type": "markdown",
            "sourceId": "v15_replay",
            "body": (
                "## Плюс не создан июльским скачком\n\n"
                "До июля правило дало **+73,5 тыс. ₽**, в июле — ещё **+9,6 тыс. ₽** экономического вклада. Продажи выросли в обоих отрезках: на 46,5 и 50,5 единицы. Июль потребовал больше дополнительного капитала, поэтому его отдача ниже, но знак результата остался положительным."
            ),
        },
        {"id": "period_chart", "type": "chart", "chartId": "period_economics_chart"},
        {
            "id": "stage_finding",
            "type": "markdown",
            "sourceId": "v15_segments",
            "body": (
                "## «Растим» помогло, но не было единственным источником эффекта\n\n"
                "Все четыре основные стадии дали положительный вклад. Больше всего добавила стадия **«Пошли продажи» — 40,6 тыс. ₽**, затем **«Растим» — 24,3 тыс. ₽**, **«Поддерживаем» — 15,9 тыс. ₽** и **«Новинка» — 2,4 тыс. ₽**. Значит, проблема была не в одной стадии: общее недоверие к pipeline было слишком широким, а экономический признак сделал его адресным."
            ),
        },
        {"id": "stage_chart", "type": "chart", "chartId": "stage_economics_chart"},
        {
            "id": "robustness",
            "type": "markdown",
            "sourceId": "v15_replay",
            "body": (
                "## Результат не держится на одном удачном SKU\n\n"
                f"Правило изменило траекторию {headline['affected_sku_count']} SKU. Один лучший SKU дал только **8,0%** валового положительного эффекта, пять лучших — **31,1%**, десять — **49,3%**. Независимый replay сошёлся с quick-v15 без расхождений по продажам и экономическому вкладу. При этом 238 SKU дали небольшой минус, поэтому правило уже положительное, но ещё не окончательное."
            ),
        },
        {
            "id": "strict_acceptance",
            "type": "markdown",
            "sourceId": "v15_summary",
            "body": (
                "## Улучшение модели ещё не равно выполненной цели\n\n"
                f"Относительно факта v15 держит на **{_number_ru(_number(headline['strict_capital_advantage_rub']) / 1_000_000, 2)} млн ₽** меньше среднего капитала, но не достигает фактических продаж, валовой прибыли и GMROI. Из исходного разрыва контроля по продажам закрыто только **{_number_ru(_number(headline['control_gap_recovered_share']) * 100, 2)}%**. Поэтому результат — подтверждённая полезная деталь модели, а не разрешение на автозаказ."
            ),
        },
        {"id": "acceptance_table_block", "type": "table", "tableId": "acceptance_table"},
        {
            "id": "next_steps",
            "type": "markdown",
            "body": (
                "## Что делать дальше\n\n"
                "1. Сохранить порог `маржа/себестоимость ≥ 0,5` как базовую часть следующего challenger; не возвращать количественный economic cap.\n"
                "2. Следующим коротким тестом добавить только признак риска конкретной партии: сильнее снижать доверие к pipeline, если поставка уже просрочена или её остаточный срок вышел за ожидаемый.\n"
                "3. Проверять новый вариант отдельно до июля и за июль и повторять SKU-replay; не отбирать отдельные SKU по будущему результату.\n"
                "4. Только после прохождения строгого критерия переходить к отдельному forward shadow без создания заказов."
            ),
        },
        {
            "id": "questions",
            "type": "markdown",
            "body": (
                "## Что ещё нужно выяснить\n\n"
                "- Достаточно ли в frozen-данных даты обещанного прихода каждой партии, чтобы честно определить просрочку без look-ahead?\n"
                "- Следует ли после просрочки использовать 90% pipeline или величину, рассчитанную из исторической надёжности конкретного поставщика?\n"
                "- Нужно ли отдельно защищать высокий дефицитный резерв при том же пороге маржи?"
            ),
        },
        {
            "id": "caveats",
            "type": "markdown",
            "body": (
                "## Ограничения и допущения\n\n"
                "Порог 0,5 выбран после exploratory-анализа того же периода, поэтому возможна подгонка; разрез июля снижает, но не устраняет этот риск. Стадийный график использует стадию на начало периода, тогда как само правило применялось по текущим данным каждой даты. Фактический незарегистрированный спрос неизвестен и оценивается через frozen-сигналы сайта, КМП4 и резерва. Все ручные рекомендации в симуляции считаются принятыми. PDF, production-заказы, внешние записи и деплой не выполнялись."
            ),
        },
    ]
    return {
        "surface": "report",
        "manifest": {
            "version": 1,
            "surface": "report",
            "title": title,
            "description": "Итоги динамического сегментного quick-v15 автозаказа дисплеев.",
            "generatedAt": analysis["generated_at"],
            "filters": [],
            "cards": cards,
            "charts": charts,
            "tables": tables,
            "sources": sources,
            "blocks": blocks,
        },
        "snapshot": {
            "version": 1,
            "generatedAt": analysis["generated_at"],
            "status": "ready",
            "datasets": {
                "headline": [
                    {
                        "economic_delta_rub": _number(headline["economic_delta_rub"]),
                        "sales_delta_qty": _number(headline["sales_delta_qty"]),
                        "capital_delta_rub": _number(headline["capital_delta_rub"]),
                        "control_gap_recovered_share": _number(
                            headline["control_gap_recovered_share"]
                        ),
                    }
                ],
                "scenario_comparison": analysis["scenario_rows"],
                "period_comparison": analysis["period_rows"],
                "stage_comparison": analysis["stage_rows"],
                "acceptance_comparison": analysis["acceptance_rows"],
            },
            "accessIssues": [],
        },
        "sources": sources,
        "package_info": {"originUrl": "artifact://display-auto-order-base-pipeline-v15"},
    }


def build_lot_risk_artifact(analysis: Mapping[str, Any]) -> dict[str, Any]:
    headline = analysis["headline"]
    paths = analysis["source_paths"]
    generated_at = analysis["generated_at"]

    def source(
        source_id: str,
        label: str,
        path: str,
        description: str,
    ) -> dict[str, Any]:
        if path.endswith(".csv"):
            sql = f"SELECT * FROM read_csv_auto('{path}', header=true);"
        elif path.endswith(".md"):
            sql = f"SELECT content FROM read_text('{path}');"
        else:
            sql = f"SELECT * FROM read_json_auto('{path}');"
        return {
            "id": source_id,
            "label": label,
            "path": path,
            "query": {
                "engine": "DuckDB SQL over frozen CSV/JSON artifacts",
                "language": "sql",
                "sql": sql,
                "description": description,
                "filters": [
                    "Период 2026-02-01 — 2026-07-31",
                    "Все 2 605 SKU предмета Дисплеи",
                    "Только frozen-файлы; production-запросы отсутствуют",
                ],
                "metric_definitions": [
                    "Экономический вклад = валовая прибыль минус стоимость содержания среднего запаса за период.",
                    "Средний капитал = средняя дневная стоимость модельного остатка.",
                    "Дополнительные продажи = обслуженные записанные продажи минус тот же показатель frozen-контроля.",
                ],
                "tables_used": [path],
            },
        }

    sources = [
        source(
            "v16_summary",
            "Frozen quick-v16: риск конкретной партии",
            paths["v16_summary"],
            "Сравнивает контроль, границу P50 и границу P75.",
        ),
        source(
            "v15_summary",
            "Frozen quick-v15: динамический допуск по марже",
            paths["v15_summary"],
            "Даёт предыдущий подтверждённый вариант без риска конкретной партии.",
        ),
        source(
            "v16_replay",
            "Независимый SKU-replay quick-v16 P75",
            paths["v16_replay"],
            "Повторно считает контроль и P75 с детализацией по SKU и периодам.",
        ),
        source(
            "v16_segments",
            "Стадии независимого replay quick-v16 P75",
            paths["v16_segments"],
            "Агрегирует эффект P75 по стадии SKU на начало периода.",
        ),
        source(
            "report_analysis",
            "Сверенное сравнение quick-v15 и quick-v16",
            paths["report_analysis"],
            "Объединяет frozen-метрики, replay, периоды и стадии.",
        ),
        source(
            "method_policy",
            "Каноническая методика жизненного цикла ассортимента",
            paths["method_policy"],
            "Фиксирует quick-v16, критерий успеха и запрет production.",
        ),
    ]

    charts = [
        {
            "id": "scenario_economics_chart",
            "title": "Экономический вклад вариантов pipeline",
            "subtitle": "Относительно одного frozen-контроля, февраль–июль 2026 года, ₽.",
            "showDescription": True,
            "type": "bar",
            "dataset": "scenario_comparison",
            "sourceId": "report_analysis",
            "encodings": {
                "x": {"field": "chart_label", "type": "nominal", "label": "Правило"},
                "y": {
                    "field": "economic_delta_rub",
                    "type": "quantitative",
                    "label": "Δ экономический вклад, ₽",
                },
                "tooltip": [
                    {
                        "field": "sales_delta_qty",
                        "type": "quantitative",
                        "label": "Δ продажи, шт.",
                    },
                    {
                        "field": "capital_delta_rub",
                        "type": "quantitative",
                        "label": "Δ средний капитал, ₽",
                        "format": "currency",
                    },
                ],
            },
            "valueFormat": "currency",
            "layout": "full",
            "palette": {"kind": "categorical"},
            "referenceLines": [{"axis": "y", "value": 0, "label": "Ноль", "style": "dashed"}],
        },
        {
            "id": "period_increment_chart",
            "title": "Дополнительный эффект P75 поверх v15 по периодам",
            "subtitle": "Февраль–июнь и июль показаны отдельно, ₽.",
            "showDescription": True,
            "type": "bar",
            "dataset": "period_comparison",
            "sourceId": "v16_replay",
            "encodings": {
                "x": {"field": "period_label", "type": "ordinal", "label": "Период"},
                "y": {
                    "field": "incremental_economic_vs_v15_rub",
                    "type": "quantitative",
                    "label": "Δ к v15, ₽",
                },
                "tooltip": [
                    {
                        "field": "incremental_sales_vs_v15_qty",
                        "type": "quantitative",
                        "label": "Δ продажи к v15, шт.",
                    },
                    {
                        "field": "incremental_capital_vs_v15_rub",
                        "type": "quantitative",
                        "label": "Δ капитал к v15, ₽",
                        "format": "currency",
                    },
                ],
            },
            "valueFormat": "currency",
            "layout": "full",
            "palette": {"kind": "categorical"},
            "referenceLines": [
                {"axis": "y", "value": 0, "label": "Без изменения", "style": "dashed"}
            ],
        },
        {
            "id": "stage_increment_chart",
            "title": "Дополнительный эффект P75 поверх v15 по стадиям",
            "subtitle": "Стадия SKU на начало frozen-периода, ₽.",
            "showDescription": True,
            "type": "bar",
            "dataset": "stage_comparison",
            "sourceId": "v16_segments",
            "encodings": {
                "x": {"field": "stage_label", "type": "ordinal", "label": "Стадия"},
                "y": {
                    "field": "incremental_economic_vs_v15_rub",
                    "type": "quantitative",
                    "label": "Δ к v15, ₽",
                },
                "tooltip": [
                    {
                        "field": "incremental_sales_vs_v15_qty",
                        "type": "quantitative",
                        "label": "Δ продажи к v15, шт.",
                    },
                    {
                        "field": "affected_sku_count",
                        "type": "quantitative",
                        "label": "Затронуто SKU",
                    },
                ],
            },
            "valueFormat": "currency",
            "layout": "full",
            "palette": {"kind": "categorical"},
            "referenceLines": [
                {"axis": "y", "value": 0, "label": "Без изменения", "style": "dashed"}
            ],
        },
    ]
    tables = [
        {
            "id": "scenario_table",
            "title": "Точное сравнение v15, P50 и P75",
            "subtitle": "Изменения относительно frozen-контроля за февраль–июль 2026 года.",
            "dataset": "scenario_comparison",
            "sourceId": "report_analysis",
            "defaultSort": {"field": "economic_delta_rub", "direction": "desc"},
            "columns": [
                {"field": "scenario_label", "label": "Правило", "type": "text"},
                {
                    "field": "sales_delta_qty",
                    "label": "Δ продажи, шт.",
                    "format": "number",
                    "movement": True,
                },
                {
                    "field": "gross_profit_delta_rub",
                    "label": "Δ валовая прибыль",
                    "format": "currency",
                    "movement": True,
                },
                {
                    "field": "capital_delta_rub",
                    "label": "Δ средний капитал",
                    "format": "currency",
                    "movement": True,
                },
                {
                    "field": "ending_inventory_delta_qty",
                    "label": "Δ остаток, шт.",
                    "format": "number",
                    "movement": True,
                },
                {
                    "field": "economic_delta_rub",
                    "label": "Δ экономический вклад",
                    "format": "currency",
                    "movement": True,
                },
            ],
        },
        {
            "id": "lot_risk_table",
            "title": "Насколько часто срабатывал риск партии",
            "subtitle": "Только medium-confidence pipeline, прошедший порог маржа/себестоимость ≥ 0,5.",
            "dataset": "lot_risk_diagnostics",
            "sourceId": "v16_summary",
            "columns": [
                {"field": "profile", "label": "Профиль", "type": "text"},
                {"field": "boundary", "label": "Граница риска", "type": "text"},
                {
                    "field": "positive_evaluations",
                    "label": "Пересчёты с риском",
                    "format": "number",
                },
                {
                    "field": "risky_pipeline_qty",
                    "label": "Рискованный pipeline, шт.",
                    "format": "number",
                },
                {
                    "field": "extra_pipeline_reduction_qty",
                    "label": "Доп. недоверие, шт.",
                    "format": "number",
                },
                {
                    "field": "incremental_sales_vs_v15_qty",
                    "label": "Δ продажи к v15",
                    "format": "number",
                    "movement": True,
                },
                {
                    "field": "incremental_economic_vs_v15_rub",
                    "label": "Δ вклад к v15",
                    "format": "currency",
                    "movement": True,
                },
            ],
        },
        {
            "id": "acceptance_table",
            "title": "Проверка строгого критерия",
            "subtitle": "Факт, контроль, v15 и лучший v16 P75 на одном frozen-периоде.",
            "dataset": "acceptance_comparison",
            "sourceId": "v16_summary",
            "defaultSort": {"field": "served_observed_qty", "direction": "desc"},
            "columns": [
                {"field": "scenario", "label": "Вариант", "type": "text"},
                {
                    "field": "served_observed_qty",
                    "label": "Продажи, шт.",
                    "format": "number",
                },
                {
                    "field": "observed_fill_rate",
                    "label": "Fill rate",
                    "format": "percent",
                },
                {
                    "field": "gross_profit_rub",
                    "label": "Валовая прибыль",
                    "format": "currency",
                },
                {
                    "field": "average_inventory_value_rub",
                    "label": "Средний капитал",
                    "format": "currency",
                },
                {
                    "field": "gmroi_annualized",
                    "label": "GMROI годовой",
                    "format": "number",
                },
                {"field": "strict_result", "label": "Строгий итог", "type": "text"},
            ],
        },
    ]

    incremental_economic = _number_ru(
        _decimal(headline["incremental_economic_vs_v15_rub"]) / Decimal("1000"), 1
    )
    incremental_profit = _number_ru(
        _decimal(headline["incremental_gross_profit_vs_v15_rub"]) / Decimal("1000"), 1
    )
    incremental_capital = _number_ru(
        _decimal(headline["incremental_capital_vs_v15_rub"]) / Decimal("1000"), 1
    )
    return {
        "surface": "report",
        "manifest": {
            "version": 1,
            "surface": "report",
            "title": "Автозаказ дисплеев: риск партии почти не изменил результат",
            "description": "Итоги quick-v16 P50/P75 относительно quick-v15.",
            "generatedAt": generated_at,
            "filters": [],
            "cards": [],
            "charts": charts,
            "tables": tables,
            "sources": sources,
            "blocks": [
                {
                    "id": "title",
                    "type": "markdown",
                    "body": "# Автозаказ дисплеев: риск партии почти не изменил результат",
                },
                {
                    "id": "executive_summary",
                    "type": "markdown",
                    "sourceId": "report_analysis",
                    "body": (
                        "## Executive Summary\n\n"
                        "- **Новых продаж сверх v15 нет.** И P50, и P75 сохранили те же **+97 продаж** к контролю; причина основного провала модели не в том, что она слишком доверяла отдельным далёким партиям.\n"
                        f"- **P75 дал небольшой экономический плюс.** Относительно v15 валовая прибыль выросла ещё на **{incremental_profit} тыс. ₽**, экономический вклад — на **{incremental_economic} тыс. ₽**, средний капитал — только на **{incremental_capital} тыс. ₽**. P75 лучше P50, потому что достигает того же сервиса с меньшим запасом.\n"
                        f"- **Цель по-прежнему далеко.** До факта не хватает **{_number_ru(headline['strict_sales_gap_qty'], 2)} продажи** и **{_number_ru(_decimal(headline['strict_profit_gap_rub']) / Decimal('1000000'), 2)} млн ₽ валовой прибыли**. Production остаётся заблокированным."
                    ),
                },
                {
                    "id": "scope",
                    "type": "markdown",
                    "sourceId": "method_policy",
                    "body": (
                        "## Что именно проверили\n\n"
                        "Всё базовое правило v15 сохранено: для medium-confidence pipeline действует 95% только при текущем отношении маржи к себестоимости не ниже 0,5. v16 меняет лишь конкретную партию: если её замороженная ожидаемая дата прихода дальше текущего P50 или P75, партия считается на 90%. Партия ровно на границе не штрафуется; будущие решения не меняют прошлое."
                    ),
                },
                {
                    "id": "scenario_finding",
                    "type": "markdown",
                    "sourceId": "report_analysis",
                    "body": (
                        "## P75 лучше P50, но оба повторяют сервис v15\n\n"
                        "P50 срабатывал гораздо шире: 872 пересчёта и 12 982 единицы pipeline против 182 пересчётов и 2 036 единиц у P75. При этом оба варианта дали одинаковые +97 продаж. Более широкий P50 только добавил пять единиц конечного остатка и оказался экономически слабее P75."
                    ),
                },
                {"id": "scenario_chart", "type": "chart", "chartId": "scenario_economics_chart"},
                {"id": "scenario_table_block", "type": "table", "tableId": "scenario_table"},
                {"id": "lot_risk_table_block", "type": "table", "tableId": "lot_risk_table"},
                {
                    "id": "period_finding",
                    "type": "markdown",
                    "sourceId": "v16_replay",
                    "body": (
                        "## Небольшое улучшение возникло до июля\n\n"
                        "Весь дополнительный экономический эффект P75 поверх v15 — около 5,4 тыс. ₽ — появился в феврале–июне. В июле P75 полностью повторил v15. Значит, улучшение не объясняется июльским структурным скачком, но и не помогает модели справиться с ним."
                    ),
                },
                {"id": "period_chart", "type": "chart", "chartId": "period_increment_chart"},
                {
                    "id": "stage_finding",
                    "type": "markdown",
                    "sourceId": "v16_segments",
                    "body": (
                        "## «Растим» улучшило экономику, но не продажи\n\n"
                        "Весь дополнительный эффект v16 поверх v15 пришёлся на стадию «Растим»: около +5,4 тыс. ₽ экономического вклада. Дополнительных продаж в этой стадии не появилось, а остальные стадии не изменились. Поэтому стадийная модель работает как полезная сегментация, но новая поправка риска не устраняет главный дефицит сервиса."
                    ),
                },
                {"id": "stage_chart", "type": "chart", "chartId": "stage_increment_chart"},
                {
                    "id": "strict_acceptance",
                    "type": "markdown",
                    "sourceId": "v16_summary",
                    "body": (
                        "## Строгий критерий снова не пройден\n\n"
                        f"P75 удерживает средний капитал примерно на {_number_ru(_decimal(headline['strict_capital_advantage_rub']) / Decimal('1000000'), 2)} млн ₽ ниже факта, но продажи, валовая прибыль и fill rate остаются ниже фактических. Из исходного разрыва контроля по продажам закрыто только {_number_ru(_decimal(headline['control_gap_recovered_share']) * Decimal('100'), 2)}%. Малый положительный вклад — это улучшение настройки, а не готовое решение автозаказа."
                    ),
                },
                {"id": "acceptance_table_block", "type": "table", "tableId": "acceptance_table"},
                {
                    "id": "next_steps",
                    "type": "markdown",
                    "body": (
                        "## Что делать дальше\n\n"
                        "1. Сохранить v15 как базу. P75 можно оставить как осторожную чувствительность, но не считать новым источником продаж.\n"
                        "2. Не запускать ещё один долгий перебор процентов pipeline. Сначала разложить оставшиеся 9 717 потерянных продаж по точной причине: не было запаса, не успела поставка, min/max не сработал, стадия ограничила заказ или спрос появился быстрее прогноза.\n"
                        "3. Следующий quick-тест строить только на крупнейшей подтверждённой причине ложного отказа модели. Отдельно показать эффект до июля и в июле.\n"
                        "4. Production разрешать только после строгого backtest и отдельного forward shadow без создания заказов."
                    ),
                },
                {
                    "id": "questions",
                    "type": "markdown",
                    "body": (
                        "## Что ещё нужно выяснить\n\n"
                        "- Какая доля пропущенных продаж вообще могла быть спасена заказом, созданным по доступным на тот день данным?\n"
                        "- На каких стадиях и типах спроса сосредоточены не просто потери, а исправимые ложные отказы min/max?\n"
                        "- Появится ли в будущей frozen-выгрузке дата размещения партии и исходная обещанная дата, чтобы отличать настоящую просрочку поставщика от изначально длинного срока?"
                    ),
                },
                {
                    "id": "caveats",
                    "type": "markdown",
                    "body": (
                        "## Ограничения и допущения\n\n"
                        "В initial-pipeline нет даты размещения и первоначально обещанной даты, поэтому v16 измеряет только остаточный срок до замороженного прихода, а не настоящую историческую просрочку. P50/P75 выбраны на том же frozen-периоде и могут быть подогнаны. Разрез стадий использует стадию на начало периода, хотя решения принимались по текущей стадии каждой даты. Все ручные рекомендации в симуляции считаются принятыми. Notebook replay создан, но его интерактивное выполнение в sandbox было недоступно; сами replay-метрики пересчитаны и сошлись с quick-v16 без расхождений. PDF, production-заказы, внешние записи и деплой не выполнялись."
                    ),
                },
            ],
        },
        "snapshot": {
            "version": 1,
            "generatedAt": generated_at,
            "status": "ready",
            "datasets": {
                "scenario_comparison": analysis["scenario_rows"],
                "lot_risk_diagnostics": analysis["lot_risk_rows"],
                "period_comparison": analysis["period_rows"],
                "stage_comparison": analysis["stage_rows"],
                "acceptance_comparison": analysis["acceptance_rows"],
            },
            "accessIssues": [],
        },
        "sources": sources,
        "package_info": {"originUrl": "artifact://display-auto-order-lot-risk-v16"},
    }


def build_lot_risk_source_notes(analysis: Mapping[str, Any]) -> str:
    return (
        "# Примечания к отчёту quick-v16\n\n"
        "## Аудитория и структура\n\n"
        "- Аудитория: партнёр и владельцы бизнес-процесса.\n"
        "- Delivery mode: один переносимый HTML; PDF не создаётся.\n"
        "- Структура: Title → Executive Summary → выводы с доказательствами → "
        "следующие шаги → вопросы → ограничения.\n\n"
        "## Карта графиков\n\n"
        "| Раздел | Вопрос | Тип | Источник | Вывод |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| Сравнение правил | Улучшил ли lot-risk результат v15? | Столбцы по v15/P50/P75 | v15/v16 frozen summaries | P75 немного лучше по экономике, новых продаж нет |\n"
        "| Устойчивость по времени | Где возник инкремент P75? | Столбцы по двум периодам | period-effects.csv | Весь инкремент до июля |\n"
        "| Стадии | Помогла ли «Растим»? | Столбцы по четырём стадиям | segment-effects.csv | Только экономический инкремент «Растим», без новых продаж |\n\n"
        "## Проверки и ограничения\n\n"
        f"- Replay: `{json.dumps(analysis['reconciliation'], ensure_ascii=False)}`.\n"
        "- Графики используют нулевую базу и точные значения доступны в таблицах.\n"
        "- Реальная просрочка неизвестна: frozen initial-pipeline не содержит ordered_at и исходную обещанную дату.\n"
        "- Production, внешние записи, деплой и PDF не выполняются.\n"
    )


def build_source_notes(analysis: Mapping[str, Any]) -> str:
    return (
        "# Примечания к отчёту quick-v15\n\n"
        "## Аудитория и структура\n\n"
        "- Аудитория: партнёр и владельцы бизнес-процесса.\n"
        "- Delivery mode: один переносимый HTML; PDF не создаётся.\n"
        "- Структура: Title → Executive Summary → выводы с доказательствами → "
        "следующие шаги → вопросы → ограничения.\n\n"
        "## Карта графиков\n\n"
        "| Раздел | Вопрос | Тип | Источник | Вывод |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| Правила pipeline | Сняла ли сегментация отрицательную экономику v14? | Столбцы по трём правилам | v14/v15 frozen summaries | Порог 0,5 положительный, общий haircut отрицательный |\n"
        "| Устойчивость по времени | Создан ли плюс июлем? | Столбцы по двум периодам | period-effects.csv | Оба периода положительны |\n"
        "| Стадии | Помогла ли «Растим»? | Столбцы по четырём стадиям | segment-effects.csv | Все четыре стадии положительны |\n\n"
        "## Проверки и ограничения\n\n"
        f"- Replay: `{json.dumps(analysis['reconciliation'], ensure_ascii=False)}`.\n"
        "- Порог выбран exploratory на том же frozen-периоде; это не causal production-оценка.\n"
        "- Статический stage-разрез описательный; динамический gate использует текущие cost/margin/confidence.\n"
        "- Production, внешние записи, деплой и PDF не выполняются.\n"
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--v14-dir", type=Path)
    parser.add_argument("--v15-dir", type=Path)
    parser.add_argument("--v16-dir", type=Path)
    parser.add_argument("--v15-report-dir", type=Path)
    parser.add_argument("--replay-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    lot_risk_mode = args.v16_dir is not None or args.v15_report_dir is not None
    if lot_risk_mode:
        if args.v15_dir is None or args.v16_dir is None or args.v15_report_dir is None:
            raise SystemExit("quick-v16 report requires --v15-dir, --v16-dir and --v15-report-dir")
        analysis = build_lot_risk_analysis(
            v15_dir=args.v15_dir,
            v16_dir=args.v16_dir,
            v15_report_dir=args.v15_report_dir,
            replay_dir=args.replay_dir,
            report_analysis_path=args.output_dir / "report-analysis.json",
        )
        artifact = build_lot_risk_artifact(analysis)
        source_notes = build_lot_risk_source_notes(analysis)
    else:
        if args.v14_dir is None or args.v15_dir is None:
            raise SystemExit("quick-v15 report requires --v14-dir and --v15-dir")
        analysis = build_analysis(
            v14_dir=args.v14_dir, v15_dir=args.v15_dir, replay_dir=args.replay_dir
        )
        artifact = build_artifact(analysis)
        source_notes = build_source_notes(analysis)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "report-analysis.json").write_text(
        json.dumps(analysis, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (args.output_dir / "artifact.json").write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (args.output_dir / "REPORT-SOURCE-NOTES.md").write_text(source_notes, encoding="utf-8")
    print(
        json.dumps(
            {
                "status": "ready",
                "analysis": str(args.output_dir / "report-analysis.json"),
                "artifact": str(args.output_dir / "artifact.json"),
                "source_notes": str(args.output_dir / "REPORT-SOURCE-NOTES.md"),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
