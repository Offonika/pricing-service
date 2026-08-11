"""Diagnose recorded sales missed by the frozen display min/max control.

The task replays one persisted control scenario with daily loss instrumentation.
It reads only frozen artifacts, writes Markdown/CSV/JSON, and has no production
or purchasing side effects.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from tasks import report_display_auto_order_frozen_backtest as frozen
from tasks.analyze_display_auto_order_quick_backtest import _prepare_inputs
from tasks.build_display_auto_order_dry_run import load_auto_order_policy
from tasks.display_auto_order_backtest_preflight import load_scenario_config

ZERO = Decimal("0")


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


def _cost_band(value: Decimal) -> str:
    if value <= ZERO:
        return "unknown"
    if value < Decimal("500"):
        return "<500"
    if value < Decimal("1500"):
        return "500-1499"
    if value < Decimal("3000"):
        return "1500-2999"
    return ">=3000"


def _lead_time_band(days: int) -> str:
    if days <= 30:
        return "<=30"
    if days <= 60:
        return "31-60"
    if days <= 90:
        return "61-90"
    return ">90"


def _pipeline_band(quantity: Decimal, days_to_arrival: int | None) -> str:
    if quantity <= ZERO:
        return "none"
    if days_to_arrival is None:
        return "open_without_date"
    if days_to_arrival <= 7:
        return "arrives_1_7d"
    if days_to_arrival <= 30:
        return "arrives_8_30d"
    return "arrives_over_30d"


def loss_reason_flags(row: Mapping[str, Any]) -> dict[str, int]:
    prior_min = _decimal(row.get("prior_min_stock_qty"))
    prior_position = _decimal(row.get("prior_inventory_position_qty"))
    prior_stock = _decimal(row.get("prior_model_stock_qty"))
    prior_reserve = _decimal(row.get("prior_reserve_qty"))
    prior_pipeline = _decimal(row.get("prior_effective_model_pipeline_qty"))
    current_pipeline = _decimal(row.get("model_pipeline_qty"))
    raw_order = _decimal(row.get("prior_recommended_order_qty_raw"))
    rounded_order = _decimal(row.get("prior_recommended_order_qty"))
    p75 = int(_decimal(row.get("current_lead_time_p75_days")))
    selected_lead = int(_decimal(row.get("prior_selected_lead_time_days")))
    confidence = _clean(row.get("current_lead_time_confidence")) or "unknown"
    return {
        "pipeline_present": int(current_pipeline > ZERO),
        "pipeline_blocked_reorder": int(
            prior_pipeline > ZERO
            and prior_position > prior_min
            and prior_stock - prior_reserve <= prior_min
        ),
        "quantity_limited": int(raw_order > rounded_order),
        "zero_or_missing_forecast": int(_decimal(row.get("prior_forecast_rate_sales")) <= ZERO),
        "demand_jump_over_trigger": int(
            bool(_clean(row.get("prior_evaluation_date")))
            and prior_position > prior_min
            and rounded_order <= ZERO
        ),
        "lead_time_risk": int(
            (selected_lead > 0 and p75 > selected_lead) or confidence in {"low", "unknown"}
        ),
        "zero_safety_stock": int(_decimal(row.get("prior_safety_stock_qty")) <= ZERO),
    }


def classify_loss_reason(row: Mapping[str, Any]) -> str:
    flags = loss_reason_flags(row)
    status = _clean(row.get("status"))
    if status in {"fruit", "newborn", "new_item", "sales_start"} and int(
        _decimal(row.get("launch_seed_pending"))
    ):
        return "new_sku_without_start_stock"
    if not _clean(row.get("prior_evaluation_date")):
        return "no_prior_control_state"
    if flags["quantity_limited"]:
        return "quantity_rounding_or_cap"
    if flags["pipeline_blocked_reorder"]:
        return "pipeline_counted_before_arrival"
    if flags["pipeline_present"]:
        return "replenishment_in_transit"
    if flags["zero_or_missing_forecast"]:
        return "zero_or_missing_forecast"
    if flags["demand_jump_over_trigger"]:
        return "demand_jump_over_min_trigger"
    if (
        int(_decimal(row.get("prior_triggered")))
        and _decimal(row.get("prior_recommended_order_qty")) <= ZERO
    ):
        return "trigger_without_order"
    return "base_target_underforecast"


def _enrich_loss_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    names_by_code: Mapping[str, str],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for source in rows:
        row = dict(source)
        business_date = date.fromisoformat(_clean(row["business_date"]))
        cost = _decimal(row.get("inventory_cost_per_unit_rub"))
        p75 = int(_decimal(row.get("current_lead_time_p75_days")))
        days_text = _clean(row.get("days_to_next_pipeline_arrival"))
        days_to_arrival = int(days_text) if days_text else None
        flags = loss_reason_flags(row)
        lost = _decimal(row.get("lost_observed_qty"))
        margin = _decimal(row.get("gross_margin_per_unit_rub"))
        row.update(flags)
        row.update(
            {
                "name": names_by_code.get(_clean(row.get("nomenclature_code")), ""),
                "period": "july" if business_date.month == 7 else "pre_july",
                "cost_band": _cost_band(cost),
                "lead_time_p75_band": _lead_time_band(p75),
                "lead_time_confidence": (
                    _clean(row.get("current_lead_time_confidence")) or "unknown"
                ),
                "pipeline_band": _pipeline_band(
                    _decimal(row.get("model_pipeline_qty")), days_to_arrival
                ),
                "reserve_state": (
                    "positive" if _decimal(row.get("effective_reserve_qty")) > ZERO else "zero"
                ),
                "loss_reason": classify_loss_reason(row),
                "lost_gross_margin_rub": str(lost * margin),
            }
        )
        output.append(row)
    output.sort(
        key=lambda row: (
            _clean(row.get("business_date")),
            _clean(row.get("nomenclature_code")),
        )
    )
    return output


def _aggregate_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    dimensions = (
        "loss_reason",
        "status",
        "demand_pattern_preperiod",
        "cost_band",
        "lead_time_confidence",
        "lead_time_p75_band",
        "period",
        "pipeline_band",
        "reserve_state",
    )
    total_lost = sum((_decimal(row.get("lost_observed_qty")) for row in rows), ZERO)
    output: list[dict[str, Any]] = []
    for dimension in dimensions:
        groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in rows:
            groups[_clean(row.get(dimension)) or "unknown"].append(row)
        for value, group in groups.items():
            lost = sum((_decimal(row.get("lost_observed_qty")) for row in group), ZERO)
            output.append(
                {
                    "segment_dimension": dimension,
                    "segment_value": value,
                    "lost_observed_qty": str(lost),
                    "lost_share": str(lost / total_lost if total_lost > ZERO else ZERO),
                    "lost_gross_margin_rub": str(
                        sum((_decimal(row.get("lost_gross_margin_rub")) for row in group), ZERO)
                    ),
                    "loss_event_rows": len(group),
                    "sku_count": len({_clean(row.get("nomenclature_code")) for row in group}),
                }
            )
    output.sort(
        key=lambda row: (
            _clean(row["segment_dimension"]),
            -_decimal(row["lost_observed_qty"]),
            _clean(row["segment_value"]),
        )
    )
    return output


def _top_sku_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[_clean(row.get("nomenclature_code"))].append(row)
    output: list[dict[str, Any]] = []
    for code, group in groups.items():
        lost_by_reason: dict[str, Decimal] = defaultdict(Decimal)
        lost_by_stage: dict[str, Decimal] = defaultdict(Decimal)
        for row in group:
            lost = _decimal(row.get("lost_observed_qty"))
            lost_by_reason[_clean(row.get("loss_reason"))] += lost
            lost_by_stage[_clean(row.get("status"))] += lost
        first = group[0]
        output.append(
            {
                "nomenclature_code": code,
                "name": _clean(first.get("name")),
                "lost_observed_qty": str(
                    sum((_decimal(row.get("lost_observed_qty")) for row in group), ZERO)
                ),
                "lost_gross_margin_rub": str(
                    sum((_decimal(row.get("lost_gross_margin_rub")) for row in group), ZERO)
                ),
                "loss_days": len(group),
                "first_loss_date": min(_clean(row.get("business_date")) for row in group),
                "last_loss_date": max(_clean(row.get("business_date")) for row in group),
                "main_loss_reason": max(lost_by_reason, key=lost_by_reason.get),
                "main_stage": max(lost_by_stage, key=lost_by_stage.get),
                "demand_pattern_preperiod": _clean(first.get("demand_pattern_preperiod")),
                "cost_band": _clean(first.get("cost_band")),
                "lead_time_confidence": _clean(first.get("lead_time_confidence")),
                "lead_time_p75_band": _clean(first.get("lead_time_p75_band")),
            }
        )
    output.sort(key=lambda row: -_decimal(row["lost_observed_qty"]))
    return output


def _ranked(aggregate_rows: Sequence[Mapping[str, Any]], dimension: str) -> list[dict[str, Any]]:
    return [
        dict(row) for row in aggregate_rows if _clean(row.get("segment_dimension")) == dimension
    ]


def _markdown_table(rows: Sequence[Mapping[str, Any]]) -> str:
    body = ["| Причина | Потеряно, ед. | Доля | SKU |", "| --- | ---: | ---: | ---: |"]
    for row in rows:
        body.append(
            "| {value} | {lost:,.2f} | {share:.1%} | {sku} |".format(
                value=_clean(row.get("segment_value")),
                lost=float(_decimal(row.get("lost_observed_qty"))),
                share=float(_decimal(row.get("lost_share"))),
                sku=int(row.get("sku_count") or 0),
            ).replace(",", " ")
        )
    return "\n".join(body)


def _build_markdown(summary: Mapping[str, Any]) -> str:
    headline = summary["headline"]
    causes = summary["rankings"]["loss_reason"]
    stages = summary["rankings"]["status"][:4]
    patterns = summary["rankings"]["demand_pattern_preperiod"][:5]
    return (
        "# Почему базовый min/max недообслужил записанные продажи\n\n"
        "## Короткий вывод\n\n"
        f"Контроль потерял **{float(_decimal(headline['lost_observed_qty'])):,.2f}** "
        f"единицы записанных продаж на **{headline['loss_sku_count']} SKU**. "
        "Это replay одной и той же frozen-модели: стадии и ускорительная надбавка "
        "не менялись, production-заказы не создавались.\n\n"
        "Главный смысл диагностики: одна строка потери назначена одной основной "
        "причине, а параллельные риски — товар в пути, нулевой safety stock и "
        "неопределённый срок — сохранены отдельными флагами. Поэтому таблица не "
        "выдаёт совпадение за доказанную причинность.\n\n"
        "## Основные причины\n\n"
        + _markdown_table(causes)
        + "\n\n## Где сосредоточен разрыв\n\n"
        + "По стадиям: "
        + ", ".join(
            f"`{row['segment_value']}` — {float(_decimal(row['lost_observed_qty'])):,.2f}"
            for row in stages
        ).replace(",", " ")
        + ".\n\nПо типу спроса: "
        + ", ".join(
            f"`{row['segment_value']}` — {float(_decimal(row['lost_observed_qty'])):,.2f}"
            for row in patterns
        ).replace(",", " ")
        + ".\n\n"
        f"До июля потеряно `{headline['pre_july_lost_qty']}`, в июле — "
        f"`{headline['july_lost_qty']}`. Топ-20 SKU дают "
        f"`{float(_decimal(headline['top_20_loss_share'])):.1%}` разрыва.\n\n"
        "## Ограничения\n\n"
        "Диагностика объясняет механизм внутри симулятора, но не доказывает, что "
        "каждый фактический приход поставщика повторился бы в production. Для v14 "
        "разрешена только одна гипотеза, сформулированная по признакам, известным "
        "на дату заказа; выбор SKU по будущей потере запрещён. PDF не создавался.\n"
    )


def build_analysis(
    *,
    preflight_dir: Path,
    quick_result_dir: Path,
    output_dir: Path,
    policy_json: Path,
    scenario_config_json: Path,
) -> dict[str, Any]:
    inputs = _prepare_inputs(preflight_dir)
    quick_summary = json.loads(
        (quick_result_dir / "frozen-summary.json").read_text(encoding="utf-8")
    )
    control_id = _clean(quick_summary["source_scenario_roles"]["control"])
    control_scenario = next(
        scenario for scenario in inputs["frozen_scenarios"] if scenario.scenario_id == control_id
    )
    control = frozen.simulate_scenario(
        scenario=control_scenario,
        fact_rows_by_date=inputs["fact_rows_by_date"],
        decision_rows_by_date=inputs["decision_rows_by_date"],
        initial_pipeline_rows=inputs["initial_pipeline"],
        sales_by_code=inputs["sales_by_code"],
        policy=load_auto_order_policy(policy_json),
        config=load_scenario_config(scenario_config_json),
        date_from=inputs["date_from"],
        date_to=inputs["date_to"],
        keep_detail=True,
        demand_sample_cache={},
    )
    names_by_code = {
        code: _clean(row.get("name")) for code, row in inputs["first_decision_by_code"].items()
    }
    loss_rows = _enrich_loss_rows(control.loss_rows, names_by_code=names_by_code)
    aggregate_rows = _aggregate_rows(loss_rows)
    top_skus = _top_sku_rows(loss_rows)
    total_lost = sum((_decimal(row.get("lost_observed_qty")) for row in loss_rows), ZERO)
    metric_lost = sum((metric.lost_observed_qty for metric in control.model.values()), ZERO)
    persisted = quick_summary["control_model"]
    persisted_lost = _decimal(persisted["lost_observed_qty"])
    period_index = {row["segment_value"]: row for row in _ranked(aggregate_rows, "period")}
    flag_totals = {
        flag: str(
            sum(
                (
                    _decimal(row.get("lost_observed_qty"))
                    for row in loss_rows
                    if int(row.get(flag) or 0)
                ),
                ZERO,
            )
        )
        for flag in (
            "pipeline_present",
            "pipeline_blocked_reorder",
            "quantity_limited",
            "zero_or_missing_forecast",
            "demand_jump_over_trigger",
            "lead_time_risk",
            "zero_safety_stock",
        )
    }
    top_20_lost = sum((_decimal(row.get("lost_observed_qty")) for row in top_skus[:20]), ZERO)
    summary: dict[str, Any] = {
        "schema": "display_auto_order_control_gap_diagnostics.v1",
        "source_preflight_manifest_sha256": _sha256(preflight_dir / "run-manifest.json"),
        "source_quick_summary_sha256": _sha256(quick_result_dir / "frozen-summary.json"),
        "scenario_id": control_id,
        "date_from": inputs["date_from"].isoformat(),
        "date_to": inputs["date_to"].isoformat(),
        "diagnostic_only": True,
        "production_authorized": False,
        "pdf_created": False,
        "headline": {
            "lost_observed_qty": str(total_lost),
            "loss_event_rows": len(loss_rows),
            "loss_sku_count": len(top_skus),
            "lost_gross_margin_rub": str(
                sum((_decimal(row.get("lost_gross_margin_rub")) for row in loss_rows), ZERO)
            ),
            "pre_july_lost_qty": _clean(period_index.get("pre_july", {}).get("lost_observed_qty")),
            "july_lost_qty": _clean(period_index.get("july", {}).get("lost_observed_qty")),
            "top_20_loss_share": str(top_20_lost / total_lost if total_lost > ZERO else ZERO),
        },
        "rankings": {
            dimension: _ranked(aggregate_rows, dimension)
            for dimension in (
                "loss_reason",
                "status",
                "demand_pattern_preperiod",
                "cost_band",
                "lead_time_confidence",
                "lead_time_p75_band",
                "period",
                "pipeline_band",
                "reserve_state",
            )
        },
        "overlapping_risk_exposure_lost_qty": flag_totals,
        "reconciliation": {
            "loss_detail_minus_replay_metric": str(total_lost - metric_lost),
            "replay_metric_minus_persisted_control": str(metric_lost - persisted_lost),
        },
        "method": {
            "primary_reason": "one mutually exclusive reason per lost SKU-day, based on the prior causal control evaluation",
            "risk_flags": "non-exclusive exposures retained separately; they must not be summed as causes",
            "demand_pattern": "52 completed weekly buckets before the frozen period",
            "no_look_ahead": "loss classification uses prior evaluation state and frozen facts available on the loss date",
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(output_dir / "lost-sales-events.csv", loss_rows)
    _write_csv(output_dir / "lost-sales-segments.csv", aggregate_rows)
    _write_csv(output_dir / "lost-sales-top-skus.csv", top_skus)
    (output_dir / "analysis-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output_dir / "CONTROL-GAP-DIAGNOSTIC.md").write_text(
        _build_markdown(summary), encoding="utf-8"
    )
    artifact_names = (
        "analysis-summary.json",
        "CONTROL-GAP-DIAGNOSTIC.md",
        "lost-sales-events.csv",
        "lost-sales-segments.csv",
        "lost-sales-top-skus.csv",
    )
    manifest = {
        "schema": "display_auto_order_control_gap_diagnostics_manifest.v1",
        "diagnostic_only": True,
        "production_authorized": False,
        "pdf_created": False,
        "files": {name: _sha256(output_dir / name) for name in artifact_names},
    }
    (output_dir / "analysis-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return summary


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight-dir", type=Path, required=True)
    parser.add_argument("--quick-result-dir", type=Path, required=True)
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
        quick_result_dir=args.quick_result_dir,
        output_dir=args.output_dir,
        policy_json=args.auto_order_policy_json,
        scenario_config_json=args.scenario_config_json,
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
