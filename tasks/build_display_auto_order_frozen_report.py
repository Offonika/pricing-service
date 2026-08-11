"""Build the partner-facing report payload for the frozen display backtest."""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

BASE_SCENARIO_ID = "grow_cap20_p90_hold4_typical_kmp0_5_sitebalanced_base"
CONTROL_SCENARIO_ID = "typical_kmp0_5_sitebalanced_base"
ACTIVE_STAGE_LABELS = {
    "new_item": "Завезли / Новинка",
    "sales_start": "Пошли продажи",
    "sale": "Растим",
    "working": "Поддерживаем",
}
MONTH_LABELS = {
    "2026-02": "Февраль",
    "2026-03": "Март",
    "2026-04": "Апрель",
    "2026-05": "Май",
    "2026-06": "Июнь",
    "2026-07": "Июль",
}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(_clean(value) or "0")
    except (ArithmeticError, ValueError):
        return Decimal("0")


def _float(value: Any) -> float:
    return float(_decimal(value))


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _iter_csv(path: Path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        yield from csv.DictReader(handle)


def _pct(value: Any, digits: int = 2) -> str:
    return f"{_decimal(value) * Decimal('100'):.{digits}f}%".replace(".", ",")


def _money_m(value: Any, digits: int = 2) -> str:
    return f"{_decimal(value) / Decimal('1000000'):.{digits}f} млн ₽".replace(".", ",")


def _ratio(numerator: Decimal, denominator: Decimal) -> Decimal:
    return numerator / denominator if denominator else Decimal("0")


def _shorten(value: str, limit: int = 72) -> str:
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def build_analysis(preflight_dir: Path, backtest_dir: Path) -> dict[str, Any]:
    frozen = json.loads((backtest_dir / "frozen-summary.json").read_text(encoding="utf-8"))
    preflight = json.loads((preflight_dir / "run-manifest.json").read_text(encoding="utf-8"))
    actual = frozen["base_actual"]
    model = frozen["base_model"]
    control_actual = frozen["control_actual"]
    control_model = frozen["control_model"]
    if frozen["base_scenario_id"] != BASE_SCENARIO_ID:
        raise ValueError(f"unexpected base scenario: {frozen['base_scenario_id']}")

    stage_source = _read_csv(backtest_dir / "frozen-baseline-stage.csv")
    stage_pairs: dict[str, dict[str, Mapping[str, str]]] = defaultdict(dict)
    for row in stage_source:
        if row.get("scenario_id") == BASE_SCENARIO_ID:
            stage_pairs[row["status"]][row["strategy"]] = row
    stages: list[dict[str, Any]] = []
    for status, label in ACTIVE_STAGE_LABELS.items():
        pair = stage_pairs.get(status, {})
        actual_stage = pair.get("actual")
        model_stage = pair.get("model")
        if not actual_stage or not model_stage:
            continue
        stages.append(
            {
                "status": status,
                "stage_label": label,
                "potential_demand_qty": _float(model_stage["potential_demand_qty"]),
                "model_observed_fill_rate": _float(model_stage["observed_fill_rate"]),
                "model_hidden_fill_rate": _float(model_stage["hidden_fill_rate"]),
                "additional_lost_qty": _float(model_stage["lost_qty"])
                - _float(actual_stage["lost_qty"]),
                "gross_profit_delta_rub": _float(model_stage["gross_profit_rub"])
                - _float(actual_stage["gross_profit_rub"]),
                "capital_delta_rub": _float(model_stage["average_inventory_value_rub"])
                - _float(actual_stage["average_inventory_value_rub"]),
                "manual_order_lines": int(model_stage["manual_order_lines"]),
                "manual_review_created": int(model_stage["manual_review_created"]),
                "manual_review_updated": int(model_stage["manual_review_updated"]),
            }
        )

    monthly_raw: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for row in _read_csv(backtest_dir / "frozen-baseline-daily.csv"):
        if row.get("scenario_id") != BASE_SCENARIO_ID:
            continue
        month = row["business_date"][:7]
        for field in (
            "actual_observed_demand_qty",
            "actual_served_observed_qty",
            "actual_hidden_demand_qty",
            "actual_served_hidden_qty",
            "model_served_observed_qty",
            "model_served_hidden_qty",
            "model_lost_observed_qty",
            "model_lost_hidden_qty",
        ):
            monthly_raw[month][field] += _decimal(row.get(field))
    monthly: list[dict[str, Any]] = []
    monthly_chart: list[dict[str, Any]] = []
    for month in sorted(monthly_raw):
        row = monthly_raw[month]
        actual_observed_fill = _ratio(
            row["actual_served_observed_qty"], row["actual_observed_demand_qty"]
        )
        model_observed_fill = _ratio(
            row["model_served_observed_qty"], row["actual_observed_demand_qty"]
        )
        actual_hidden_fill = _ratio(
            row["actual_served_hidden_qty"], row["actual_hidden_demand_qty"]
        )
        model_hidden_fill = _ratio(row["model_served_hidden_qty"], row["actual_hidden_demand_qty"])
        monthly.append(
            {
                "month": month,
                "month_label": MONTH_LABELS.get(month, month),
                "actual_observed_fill_rate": float(actual_observed_fill),
                "model_observed_fill_rate": float(model_observed_fill),
                "actual_hidden_fill_rate": float(actual_hidden_fill),
                "model_hidden_fill_rate": float(model_hidden_fill),
                "model_lost_observed_qty": float(row["model_lost_observed_qty"]),
                "model_lost_hidden_qty": float(row["model_lost_hidden_qty"]),
            }
        )
        for strategy, fill in (
            ("Фактическая стратегия", actual_observed_fill),
            ("Модель", model_observed_fill),
        ):
            monthly_chart.append(
                {
                    "month": month,
                    "month_label": MONTH_LABELS.get(month, month),
                    "strategy_label": strategy,
                    "observed_fill_rate": float(fill),
                }
            )

    scenarios: list[dict[str, Any]] = []
    scenario_chart: list[dict[str, Any]] = []
    for row in _read_csv(backtest_dir / "frozen-scenario-summary.csv"):
        if row.get("strategy") != "model":
            continue
        order_lines = _decimal(row.get("order_lines"))
        item = {
            "scenario_id": row["scenario_id"],
            "stage_profile": row["stage_profile"],
            "profile_label": {
                "conservative": "Осторожный",
                "typical": "Типичный",
                "service": "Сервисный",
                "legacy": "Legacy",
            }.get(row["stage_profile"], row["stage_profile"]),
            "kmp4_weight": _float(row["kmp4_weight"]),
            "kmp4_label": f"КМП4 × {row['kmp4_weight'].replace('.', ',')}",
            "site_profile": row.get("site_profile") or "off",
            "site_profile_label": {
                "off": "Сайт выключен",
                "order_only": "Только заказы",
                "cautious": "Осторожный",
                "balanced": "Сбалансированный",
                "service": "Сервисный",
            }.get(row.get("site_profile") or "off", row.get("site_profile") or "off"),
            "site_order_weight": _float(row.get("site_order_weight")),
            "site_unordered_cart_weight": _float(row.get("site_unordered_cart_weight")),
            "grow_weekly_reduction_cap": _float(row.get("grow_weekly_reduction_cap")),
            "forecast_error_percentile": _float(row.get("forecast_error_percentile")),
            "grow_entry_protection_weeks": int(_decimal(row.get("grow_entry_protection_weeks"))),
            "holding_cost_scenario": row["holding_cost_scenario"],
            "total_fill_rate": _float(row["fill_rate"]),
            "observed_fill_rate": _float(row["observed_fill_rate"]),
            "hidden_fill_rate": _float(row["hidden_fill_rate"]),
            "gross_profit_rub": _float(row["gross_profit_rub"]),
            "average_inventory_value_rub": _float(row["average_inventory_value_rub"]),
            "gross_profit_delta_rub": _float(row["gross_profit_delta_rub"]),
            "capital_delta_rub": _float(row["capital_delta_rub"]),
            "economic_contribution_rub": _float(row["economic_contribution_rub"]),
            "economic_contribution_delta_rub": _float(row["economic_contribution_delta_rub"]),
            "gmroi_annualized": _float(row["gmroi_annualized"]),
            "manual_order_share": (
                _float(row["manual_order_lines"]) / float(order_lines) if order_lines else 0.0
            ),
            "safety_stock_units_ordered": _float(row["safety_stock_units_ordered"]),
            "manual_order_lines": int(_decimal(row.get("manual_order_lines"))),
            "manual_review_created": int(_decimal(row.get("manual_review_created"))),
            "manual_review_updated": int(_decimal(row.get("manual_review_updated"))),
            "acceptance_passed": _clean(row.get("acceptance_passed")) == "1",
        }
        scenarios.append(item)
        if item["grow_weekly_reduction_cap"] > 0:
            item["scenario_label"] = (
                f"{item['grow_weekly_reduction_cap'] * 100:.0f}% / "
                f"P{item['forecast_error_percentile'] * 100:.0f} / "
                f"{item['grow_entry_protection_weeks']} нед."
            )
            scenario_chart.append(item)
    control_scenario = next(row for row in scenarios if row["scenario_id"] == CONTROL_SCENARIO_ID)
    base_scenario = next(row for row in scenarios if row["scenario_id"] == BASE_SCENARIO_ID)
    for row in scenario_chart:
        row["observed_fill_delta_vs_control"] = (
            row["observed_fill_rate"] - control_scenario["observed_fill_rate"]
        )
        row["gross_profit_delta_vs_control_rub"] = (
            row["gross_profit_delta_rub"] - control_scenario["gross_profit_delta_rub"]
        )
        row["capital_delta_vs_control_rub"] = (
            row["capital_delta_rub"] - control_scenario["capital_delta_rub"]
        )
        row["capital_delta_vs_control_million_rub"] = (
            row["capital_delta_vs_control_rub"] / 1_000_000
        )
        row["observed_fill_delta_vs_control_pp"] = row["observed_fill_delta_vs_control"] * 100
        row["cap_label"] = f"снижение ≤ {row['grow_weekly_reduction_cap'] * 100:.0f}%"

    best_service = max(scenario_chart, key=lambda row: row["observed_fill_rate"])
    best_economic = max(scenario_chart, key=lambda row: row["economic_contribution_rub"])
    comparison_specs = [
        ("Фактический запас", actual),
        ("Без новой защиты", control_model),
        ("Центральный 20% / P90 / 4 нед.", model),
        ("Лучший сервис 10% / P95 / 6 нед.", best_service),
        ("Лучший баланс 10% / P90 / 2 нед.", best_economic),
    ]
    scenario_comparison: list[dict[str, Any]] = []
    seen_comparisons: set[str] = set()
    for label, row in comparison_specs:
        scenario_id = _clean(row.get("scenario_id")) or "actual"
        comparison_key = f"{label}:{scenario_id}"
        if comparison_key in seen_comparisons:
            continue
        seen_comparisons.add(comparison_key)
        scenario_comparison.append(
            {
                "scenario_label": label,
                "scenario_id": scenario_id,
                "observed_fill_rate": _float(row["observed_fill_rate"]),
                "gross_profit_rub": _float(row["gross_profit_rub"]),
                "average_inventory_value_rub": _float(row["average_inventory_value_rub"]),
                "economic_contribution_rub": _float(row["economic_contribution_rub"]),
            }
        )

    factor_effects: list[dict[str, Any]] = []
    for factor, field, values in (
        ("Предел снижения", "grow_weekly_reduction_cap", (0.1, 0.2, 0.3)),
        ("Ошибка прогноза", "forecast_error_percentile", (0.75, 0.9, 0.95)),
        ("Защита после входа", "grow_entry_protection_weeks", (2, 4, 6)),
    ):
        for value in values:
            selected = [row for row in scenario_chart if row[field] == value]
            factor_effects.append(
                {
                    "factor": factor,
                    "level": value,
                    "level_label": (
                        f"{value * 100:.0f}%"
                        if field == "grow_weekly_reduction_cap"
                        else (
                            f"P{value * 100:.0f}"
                            if field == "forecast_error_percentile"
                            else f"{value} нед."
                        )
                    ),
                    "observed_fill_delta_vs_control": sum(
                        row["observed_fill_delta_vs_control"] for row in selected
                    )
                    / len(selected),
                    "gross_profit_delta_vs_control_rub": sum(
                        row["gross_profit_delta_vs_control_rub"] for row in selected
                    )
                    / len(selected),
                    "capital_delta_vs_control_rub": sum(
                        row["capital_delta_vs_control_rub"] for row in selected
                    )
                    / len(selected),
                    "economic_contribution_delta_vs_control_rub": sum(
                        row["economic_contribution_rub"]
                        - control_scenario["economic_contribution_rub"]
                        for row in selected
                    )
                    / len(selected),
                }
            )

    period_rows = _read_csv(backtest_dir / "frozen-baseline-period.csv")

    names: dict[str, str] = {}
    for row in _read_csv(preflight_dir / "decision-inputs.csv"):
        names.setdefault(row["nomenclature_code"], row.get("name", ""))
    sku_rows = _read_csv(backtest_dir / "frozen-baseline-sku.csv")
    sku_rows.sort(key=lambda row: _decimal(row["gross_profit_delta_rub"]))
    negative_gp_total = -sum(
        (min(Decimal("0"), _decimal(row["gross_profit_delta_rub"])) for row in sku_rows),
        Decimal("0"),
    )
    top_skus: list[dict[str, Any]] = []
    for rank, row in enumerate(sku_rows[:10], start=1):
        top_skus.append(
            {
                "rank": rank,
                "code": row["nomenclature_code"],
                "name": _shorten(names.get(row["nomenclature_code"], "")),
                "model_lost_observed_qty": _float(row["model_lost_observed_qty"]),
                "model_lost_hidden_qty": _float(row["model_lost_hidden_qty"]),
                "gross_profit_delta_rub": _float(row["gross_profit_delta_rub"]),
                "capital_delta_rub": _float(row["capital_delta_rub"]),
            }
        )
    top10_loss = -sum((min(0.0, row["gross_profit_delta_rub"]) for row in top_skus), 0.0)

    trigger_counts: dict[str, int] = defaultdict(int)
    protection_reason_counts: dict[str, int] = defaultdict(int)
    protection_skus: dict[str, set[str]] = defaultdict(set)
    percentile_binding_rows = 0
    economic_binding_rows = 0
    for row in _iter_csv(backtest_dir / "frozen-baseline-decisions.csv"):
        if row.get("scenario_id") != BASE_SCENARIO_ID:
            continue
        if _decimal(row.get("recommended_order_qty")) > 0:
            trigger_counts[row.get("decision_trigger") or "unknown"] += 1
        if row.get("status") != "sale":
            continue
        reason = row.get("grow_protection_reason") or "none"
        protection_reason_counts[reason] += 1
        protection_skus[reason].add(row["nomenclature_code"])
        percentile_qty = _decimal(row.get("forecast_error_percentile_qty"))
        economic_cap = _decimal(row.get("economic_safety_cap_qty"))
        if percentile_qty < economic_cap:
            percentile_binding_rows += 1
        elif economic_cap > 0 and economic_cap <= percentile_qty:
            economic_binding_rows += 1

    entered_sale_codes: set[str] = set()
    sale_codes: set[str] = set()
    sale_demand_by_code: dict[str, Decimal] = defaultdict(Decimal)
    for row in _iter_csv(preflight_dir / "daily-facts.csv"):
        if row.get("status") != "sale":
            continue
        code = row["nomenclature_code"]
        sale_codes.add(code)
        sale_demand_by_code[code] += _decimal(row.get("observed_sales_qty"))
        if row.get("previous_status") and row.get("previous_status") != "sale":
            entered_sale_codes.add(code)
    entered_sale_demand = sum(
        (sale_demand_by_code[code] for code in entered_sale_codes), Decimal("0")
    )
    total_sale_demand = sum(sale_demand_by_code.values(), Decimal("0"))

    quality = {
        row["check"]: {
            "status": row["status"],
            "severity": row["severity"],
            "value": row["value"],
            "note": row["note"],
        }
        for row in _read_csv(preflight_dir / "source-quality.csv")
    }

    extra_lost_total = _decimal(model["lost_qty"]) - _decimal(actual["lost_qty"])
    sale_extra_lost = next(
        (_decimal(str(row["additional_lost_qty"])) for row in stages if row["status"] == "sale"),
        Decimal("0"),
    )
    order_lines = _decimal(model["order_lines"])
    manual_share = _ratio(_decimal(model["manual_order_lines"]), order_lines)
    return {
        "schema": "display_auto_order_frozen_report_analysis.v3",
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "period": {"date_from": frozen["date_from"], "date_to": frozen["date_to"]},
        "cohort": {
            "sku_count": int(quality["source_count_cohort_sku_count"]["value"]),
            "classification_run_id": preflight["classification_run_id"],
            "decision_input_rows": preflight["row_counts"]["decision_inputs"],
        },
        "acceptance": frozen["acceptance"],
        "protective_scenario_acceptance": frozen["protective_scenario_acceptance"],
        "actual": actual,
        "model": model,
        "control_actual": control_actual,
        "control_model": control_model,
        "control_scenario": control_scenario,
        "base_scenario": base_scenario,
        "best_service_scenario": best_service,
        "best_economic_scenario": best_economic,
        "headline": {
            "observed_fill_rate": _float(model["observed_fill_rate"]),
            "observed_fill_delta": _float(model["observed_fill_rate"])
            - _float(actual["observed_fill_rate"]),
            "hidden_fill_rate": _float(model["hidden_fill_rate"]),
            "hidden_fill_delta": _float(model["hidden_fill_rate"])
            - _float(actual["hidden_fill_rate"]),
            "gross_profit_delta_rub": _float(model["gross_profit_delta_rub"]),
            "capital_delta_rub": _float(model["capital_delta_rub"]),
            "economic_contribution_delta_rub": _float(model["economic_contribution_delta_rub"]),
            "observed_fill_improvement_vs_control": _float(model["observed_fill_rate"])
            - _float(control_model["observed_fill_rate"]),
            "gross_profit_improvement_vs_control_rub": _float(model["gross_profit_rub"])
            - _float(control_model["gross_profit_rub"]),
            "capital_increase_vs_control_rub": _float(model["average_inventory_value_rub"])
            - _float(control_model["average_inventory_value_rub"]),
            "best_service_fill_improvement_vs_control": best_service["observed_fill_rate"]
            - control_scenario["observed_fill_rate"],
            "best_service_profit_improvement_vs_control_rub": best_service[
                "gross_profit_delta_vs_control_rub"
            ],
            "best_service_capital_increase_vs_control_rub": best_service[
                "capital_delta_vs_control_rub"
            ],
            "manual_order_share": float(manual_share),
            "manual_review_created": int(model["manual_review_created"]),
            "manual_review_updated": int(model["manual_review_updated"]),
            "manual_review_creation_reduction": 1
            - int(model["manual_review_created"]) / max(1, int(model["manual_order_lines"])),
            "extra_lost_total_qty": float(extra_lost_total),
            "sale_extra_lost_qty": float(sale_extra_lost),
            "sale_extra_lost_share": float(_ratio(sale_extra_lost, extra_lost_total)),
            "top10_negative_gp_share": float(
                Decimal(str(top10_loss)) / negative_gp_total if negative_gp_total else Decimal("0")
            ),
            "hidden_kmp4_qty": _float(model.get("hidden_kmp4_qty")),
            "hidden_site_order_qty": _float(model.get("hidden_site_order_qty")),
            "hidden_site_cart_qty": _float(model.get("hidden_site_cart_qty")),
            "hidden_reserve_backlog_qty": _float(model.get("hidden_reserve_backlog_qty")),
            "entered_sale_demand_share": float(_ratio(entered_sale_demand, total_sale_demand)),
            "sale_sku_count": len(sale_codes),
            "entered_sale_sku_count": len(entered_sale_codes),
            "economic_safety_binding_share": economic_binding_rows
            / max(1, economic_binding_rows + percentile_binding_rows),
        },
        "stages": stages,
        "monthly": monthly,
        "monthly_chart": monthly_chart,
        "scenarios": scenarios,
        "scenario_chart": scenario_chart,
        "scenario_comparison": scenario_comparison,
        "factor_effects": factor_effects,
        "period_sensitivity": period_rows,
        "top_skus": top_skus,
        "trigger_counts": dict(trigger_counts),
        "protection_reason_counts": dict(protection_reason_counts),
        "protection_sku_counts": {key: len(values) for key, values in protection_skus.items()},
        "source_quality": quality,
        "site_export": preflight.get("site_export", {}),
        "method": frozen["method"],
    }


def build_markdown(analysis: Mapping[str, Any]) -> str:
    headline = analysis["headline"]
    model = analysis["model"]
    actual = analysis["actual"]
    control = analysis["control_model"]
    best_service = analysis["best_service_scenario"]
    best_economic = analysis["best_economic_scenario"]
    quality = analysis["source_quality"]
    passed_count = analysis["protective_scenario_acceptance"]["passed_count"]
    periods: dict[str, dict[str, Mapping[str, Any]]] = defaultdict(dict)
    for row in analysis["period_sensitivity"]:
        periods[row["period"]][row["strategy"]] = row
    pre_july_model = periods.get("pre_july", {}).get("model", {})
    july_model = periods.get("july", {}).get("model", {})
    site_mapping = analysis.get("site_export", {}).get("mapping_stats", {})
    return f"""# Автозаказ дисплеев: защита «Растим» улучшила результат, но не закрыла дефицит

## Executive Summary

- **Автозаказ включать нельзя:** строгий критерий не прошёл ни один из `18` защитных вариантов (`{passed_count}/18`). Центральный вариант обслужил **{_pct(model['observed_fill_rate'])}** записанных продаж против **{_pct(actual['observed_fill_rate'])}** у фактического запаса.
- **Защита работает в правильную сторону, но слабо:** центральный вариант вернул {_float(model['served_observed_qty']) - _float(control['served_observed_qty']):.0f} продаж и {_money_m(headline['gross_profit_improvement_vs_control_rub'])} валовой прибыли относительно прежней модели, но до факта всё ещё не хватает {_money_m(-headline['gross_profit_delta_rub'])}.
- **Лучший сервис дал только частичное восстановление:** `10% + P95 + 6 недель` вернул {_pct(best_service['observed_fill_rate'] - _float(control['observed_fill_rate']), 3)} сервиса и {_money_m(best_service['gross_profit_delta_vs_control_rub'])} прибыли, потребовав ещё {_money_m(best_service['capital_delta_vs_control_rub'])} капитала.
- **Причина ограниченного эффекта понятна:** период после входа покрывает лишь {_pct(headline['entered_sale_demand_share'], 1)} спроса «Растим», а в {_pct(headline['economic_safety_binding_share'], 1)} применимых решений страховой запас ограничивает экономика, поэтому `P90` и `P95` почти не различаются.

## Что именно проверяли

Период — с 1 февраля по 31 июля 2026 года, когорта — {analysis['cohort']['sku_count']} SKU предмета «Дисплеи». На каждую дату стадия восстановлена только по продажам. Сайт, КМП4 и резерв не переводят товар между стадиями, а уточняют потребность.

Сравнивались прежняя модель без новой защиты, центральный вариант `20% + P90 + 4 недели` и ещё `17` сбалансированных сочетаний. Во всех защитных вариантах зафиксированы типичный старт новинок, КМП4 `0,5`, сайт `balanced` и базовая стоимость запаса — менялись только три правила «Растим».

## Защита вернула часть продаж, но не приблизила модель к факту достаточно

Центральный вариант повысил сервис обычных продаж с **{_pct(control['observed_fill_rate'])}** до **{_pct(model['observed_fill_rate'])}**. Валовая прибыль выросла с {_money_m(control['gross_profit_rub'])} до {_money_m(model['gross_profit_rub'])}, а средний капитал — с {_money_m(control['average_inventory_value_rub'])} до {_money_m(model['average_inventory_value_rub'])}.

Даже лучший по сервису вариант обслужил только **{_pct(best_service['observed_fill_rate'])}** записанных продаж. Лучший экономический баланс — `10% + P90 + 2 недели`: он почти не ухудшил экономический вклад относительно прежней модели ({_money_m(best_economic['economic_contribution_rub'] - analysis['control_scenario']['economic_contribution_rub'])}), но строгие условия по прибыли и сервису всё равно не выполнил.

## Почему три защиты не решили проблему

1. **Защитный период затрагивает меньшую часть оборота.** В тесте в «Растим» находились {headline['sale_sku_count']} SKU, но внутри периода в эту стадию вошли {headline['entered_sale_sku_count']}. На них пришлось только {_pct(headline['entered_sale_demand_share'], 1)} продаж стадии; остальные {_pct(1 - headline['entered_sale_demand_share'], 1)} принадлежали товарам, которые уже были в «Растим» к началу теста.
2. **Ограничение снижения сохраняет прежний уровень, но не исправляет изначально низкую цель.** Самый жёсткий вариант `10%` лучше `20%` и `30%`, однако даже он лишь медленнее уменьшает запас. Если рассчитанный min/max уже недостаточен, защита удерживает недостаточную величину.
3. **Экономический предел сильнее выбранного percentile.** В {_pct(headline['economic_safety_binding_share'], 1)} решений с положительной защитой именно экономический расчёт оказался верхней границей. Поэтому повышение `P90` до `P95` почти не добавляет запас: математический уровень растёт, но заказ обрезается раньше.
4. **Проблема остаётся широкой.** На «Растим» всё ещё приходится {_pct(headline['sale_extra_lost_share'], 1)} дополнительного дефицита центрального варианта. Это не ошибка нескольких карточек.

## Ручная очередь стала управляемее по числу карточек

Центральный сценарий содержит {model['manual_order_lines']} строк заказа с ручным принятием, но создаёт только {headline['manual_review_created']} карточек `manual_review`; остальные {headline['manual_review_updated']} события обновляют уже существующую карточку SKU. Число новых карточек уменьшено на {_pct(headline['manual_review_creation_reduction'], 1)} по сравнению с созданием отдельной карточки на каждую строку.

Обновления выполняются автоматически и не равны ручным действиям закупщика. Перед forward shadow стоит добавить порог материальности обновления, чтобы не менять карточку из-за незначительного пересчёта количества.

## До июля и июль отдельно

- До июля сервис модели: **{_pct(pre_july_model.get('fill_rate', 0))}**, валовая прибыль: **{_money_m(pre_july_model.get('gross_profit_rub', 0))}**.
- За июль сервис модели: **{_pct(july_model.get('fill_rate', 0))}**, валовая прибыль: **{_money_m(july_model.get('gross_profit_rub', 0))}**.

Июльский скачок корзин не удалён из расчёта. Он показан отдельно, чтобы партнёр видел, насколько итог чувствителен к изменению структуры сайта.

## Что делать дальше

1. **Не включать автоматические заказы.** Оставить только расчёт рекомендаций без отправки заказов.
2. **Следующим тестом дать сервисный минимум всем SKU стадии «Растим», а не только недавно вошедшим.** Кандидат — нижняя граница запаса по прошлому спросу на срок поставки, рассчитанная без будущих данных.
3. **Развести сервис и экономический фильтр в сценариях.** Проверить `P90/P95` как обязательный сервисный уровень, а экономику показывать отдельным предупреждением; параллельно протестировать стоимость дефицита с учётом потери клиента, а не только маржи одной продажи.
4. **Проверить надёжность товара в пути.** Отдельный сценарий должен учитывать опоздание или недопоставку подтверждённого pipeline; текущая модель считает его свободным полностью.
5. **Обновлять `manual_review` только при существенном изменении.** Например, при смене причины или количества заказа выше отдельного порога, который нужно согласовать до реализации.
6. **Forward shadow пока не запускать:** сначала новый frozen-backtest должен пройти прежний строгий критерий.

## Открытые вопросы

- Готовы ли мы в следующем backtest проверить обязательный сервисный уровень `P90/P95`, который не обрезается текущим экономическим лимитом?
- Как оценить полную цену дефицита: только потерянная маржа или также риск ухода клиента и срочной закупки?
- Как оценивать замену одного качества/версии дисплея другим, чтобы не считать весь дефицит потерянной продажей?
- Какой порог изменения количества должен обновлять `manual_review`?

## Ограничения

- Фактический fill rate обычных продаж равен 100% по определению: видны только состоявшиеся продажи. Незарегистрированный спрос оценивается через несколько косвенных источников.
- В frozen-файл вошло {site_mapping.get('mapped_row_count', '—')} сопоставленных строк сайта; события вне когорты не распределялись догадкой.
- Найдены {quality['negative_register_balances']['value']} отрицательных дневных балансов регистра и {quality['unit_economics_coverage']['value']} строк решений без себестоимости. Отрицательный резерв теперь безопасно обнулён для доступности, но причина в 1С остаётся вопросом качества данных.
- В сценарии все положительные ручные рекомендации считаются принятыми. Реальный результат без этой дисциплины будет хуже.
- Production-заказы и внешние записи не создавались.
"""


def _source(source_id: str, label: str, path: str) -> dict[str, Any]:
    reader = "read_json_auto" if path.endswith(".json") else "read_csv_auto"
    sql = f"SELECT *\nFROM {reader}('{path}');"
    return {
        "id": source_id,
        "label": label,
        "path": path,
        "query": {
            "engine": "DuckDB SQL over frozen CSV/JSON artifacts",
            "language": "sql",
            "sql": sql,
            "description": "Читает сохранённый PASS preflight или frozen-backtest; отчёт агрегирует эти строки локальным детерминированным Python-кодом без обращения к БД.",
            "filters": [
                "Период 2026-02-01 — 2026-07-31",
                "Все SKU предмета Дисплеи",
                "Базовый сценарий typical / КМП4 0,5 / site balanced / cost base",
            ],
            "metric_definitions": [
                "Fill rate = обслуженный потенциальный спрос / потенциальный спрос.",
                "Средний капитал = средняя дневная стоимость модельного остатка.",
                "Валовая прибыль = обслуженное количество × историческая валовая маржа единицы.",
            ],
            "tables_used": [path],
        },
    }


def build_artifact(analysis: Mapping[str, Any]) -> dict[str, Any]:
    title = "Автозаказ дисплеев: защита «Растим» улучшила результат, но не закрыла дефицит"
    headline = analysis["headline"]
    stages = analysis["stages"]
    sources = [
        _source("frozen_summary", "Итог frozen-backtest", "frozen-summary.json"),
        _source(
            "scenario_summary",
            "Сценарная сетка frozen-backtest",
            "frozen-scenario-summary.csv",
        ),
        _source(
            "stage_summary",
            "Результат базового сценария по историческим стадиям",
            "frozen-baseline-stage.csv",
        ),
        _source(
            "daily_summary",
            "Дневной результат базового сценария",
            "frozen-baseline-daily.csv",
        ),
        _source(
            "sku_summary",
            "Результат базового сценария по SKU",
            "frozen-baseline-sku.csv",
        ),
        _source(
            "preflight_manifest",
            "PASS preflight и контрольные суммы источников",
            "next-stage-model-preflight-grow-protection-v3/run-manifest.json",
        ),
        _source(
            "preflight_daily",
            "Дневные frozen-факты и переходы исторических стадий",
            "next-stage-model-preflight-grow-protection-v3/daily-facts.csv",
        ),
        _source(
            "decision_detail",
            "Детализация решений и срабатывания защит",
            "frozen-baseline-decisions.csv",
        ),
    ]
    headline_dataset = [
        {
            **dict(headline),
            "gross_profit_delta_million_rub": _float(
                _decimal(headline["gross_profit_delta_rub"]) / Decimal("1000000")
            ),
            "capital_delta_million_rub": _float(
                _decimal(headline["capital_delta_rub"]) / Decimal("1000000")
            ),
            "gross_profit_improvement_vs_control_million_rub": _float(
                _decimal(headline["gross_profit_improvement_vs_control_rub"]) / Decimal("1000000")
            ),
            "capital_increase_vs_control_million_rub": _float(
                _decimal(headline["capital_increase_vs_control_rub"]) / Decimal("1000000")
            ),
            "passed_scenario_count": analysis["protective_scenario_acceptance"]["passed_count"],
            "evaluated_scenario_count": analysis["protective_scenario_acceptance"][
                "evaluated_count"
            ],
        }
    ]
    scenario_base = sorted(analysis["scenario_chart"], key=lambda row: row["scenario_id"])
    cards = [
        {
            "id": "acceptance_card",
            "description": "Число защитных вариантов, выполнивших все три строгих условия.",
            "dataset": "headline",
            "sourceId": "frozen_summary",
            "metrics": [
                {
                    "label": "Прошли acceptance",
                    "field": "passed_scenario_count",
                    "format": "number",
                },
                {
                    "label": "проверено",
                    "field": "evaluated_scenario_count",
                    "format": "number",
                },
            ],
        },
        {
            "id": "observed_fill_card",
            "description": "Доля записанных продаж центрального варианта и улучшение к прежней модели.",
            "dataset": "headline",
            "sourceId": "frozen_summary",
            "metrics": [
                {
                    "label": "Сервис обычных продаж",
                    "field": "observed_fill_rate",
                    "format": "percent",
                },
                {
                    "label": "к прежней модели",
                    "field": "observed_fill_improvement_vs_control",
                    "format": "percent",
                    "signed": True,
                },
            ],
        },
        {
            "id": "profit_delta_card",
            "description": "Разрыв валовой прибыли к факту и возврат относительно прежней модели.",
            "dataset": "headline",
            "sourceId": "frozen_summary",
            "metrics": [
                {
                    "label": "Δ прибыль к факту, млн ₽",
                    "field": "gross_profit_delta_million_rub",
                    "format": "number",
                    "signed": True,
                },
                {
                    "label": "возвращено к прежней модели, млн ₽",
                    "field": "gross_profit_improvement_vs_control_million_rub",
                    "format": "number",
                    "signed": True,
                },
            ],
        },
        {
            "id": "manual_review_card",
            "description": "Одна карточка создаётся на SKU; следующие сигналы обновляют её.",
            "dataset": "headline",
            "sourceId": "frozen_summary",
            "metrics": [
                {
                    "label": "Новых manual_review",
                    "field": "manual_review_created",
                    "format": "number",
                },
                {
                    "label": "меньше новых карточек",
                    "field": "manual_review_creation_reduction",
                    "format": "percent",
                },
            ],
        },
    ]
    charts = [
        {
            "id": "stage_loss_chart",
            "title": "Дополнительный дефицит по стадиям",
            "subtitle": "Базовый сценарий, модель минус фактическая стратегия, штук.",
            "showDescription": True,
            "intent": "comparison",
            "question": "На какой исторической стадии возникает основной дефицит?",
            "rationale": "Горизонтальные столбцы показывают точный вклад взаимоисключающих стадий.",
            "comparisonContext": {
                "baseline": "Фактическая стратегия",
                "grain": "Историческая стадия SKU-дня",
                "unit": "units",
            },
            "type": "bar",
            "dataset": "stages",
            "sourceId": "stage_summary",
            "encodings": {
                "x": {
                    "field": "stage_label",
                    "type": "nominal",
                    "label": "Историческая стадия",
                },
                "y": {
                    "field": "additional_lost_qty",
                    "type": "quantitative",
                    "label": "Дополнительный дефицит, шт.",
                },
                "tooltip": [
                    {
                        "field": "gross_profit_delta_rub",
                        "type": "quantitative",
                        "label": "Δ валовая прибыль, ₽",
                    },
                    {
                        "field": "capital_delta_rub",
                        "type": "quantitative",
                        "label": "Δ капитал, ₽",
                    },
                ],
            },
            "valueFormat": "number",
            "layout": "full",
            "palette": {"kind": "categorical"},
        },
        {
            "id": "monthly_service_chart",
            "title": "Сервис обычных продаж по месяцам",
            "subtitle": "Февраль–июль 2026 года; доля записанных продаж.",
            "showDescription": True,
            "intent": "trend",
            "question": "Когда модель начинает отставать от фактического запаса?",
            "rationale": "Сгруппированные месячные столбцы показывают нарастание разрыва без ложной точности дневной линии.",
            "comparisonContext": {
                "baseline": "Фактическая стратегия",
                "grain": "Месяц",
                "unit": "fraction",
            },
            "type": "bar",
            "dataset": "monthly_service",
            "sourceId": "daily_summary",
            "encodings": {
                "x": {
                    "field": "month_label",
                    "type": "ordinal",
                    "label": "Месяц",
                },
                "y": {
                    "field": "observed_fill_rate",
                    "type": "quantitative",
                    "label": "Обслужено обычных продаж",
                },
                "color": {
                    "field": "strategy_label",
                    "type": "nominal",
                    "label": "Стратегия",
                },
            },
            "valueFormat": "percent",
            "layout": "full",
            "palette": {"kind": "semantic"},
            "legend": {"position": "bottom", "sort": "spec"},
            "settings": {"groupMode": "grouped"},
        },
        {
            "id": "protection_tradeoff_chart",
            "title": "18 защитных вариантов: прирост сервиса и капитала",
            "subtitle": "Модель с защитой минус прежняя модель; февраль–июль 2026 года.",
            "showDescription": True,
            "intent": "relationship",
            "question": "Какой прирост сервиса покупает каждый дополнительный миллион капитала?",
            "rationale": "Точки показывают экономический обмен для всех 18 вариантов на одном масштабе.",
            "comparisonContext": {
                "baseline": "Прежняя модель без новой защиты",
                "grain": "Сценарий",
                "unit": "percentage points and million RUB",
            },
            "type": "scatter",
            "dataset": "protection_scenarios",
            "sourceId": "scenario_summary",
            "encodings": {
                "x": {
                    "field": "capital_delta_vs_control_million_rub",
                    "type": "quantitative",
                    "label": "Дополнительный капитал, млн ₽",
                },
                "y": {
                    "field": "observed_fill_delta_vs_control_pp",
                    "type": "quantitative",
                    "label": "Прирост сервиса, п.п.",
                },
                "color": {
                    "field": "cap_label",
                    "type": "nominal",
                    "label": "Предел недельного снижения",
                },
                "tooltip": [
                    {
                        "field": "scenario_label",
                        "type": "nominal",
                        "label": "Вариант",
                    },
                    {
                        "field": "gross_profit_delta_vs_control_rub",
                        "type": "quantitative",
                        "label": "Возврат прибыли к прежней модели, ₽",
                    },
                    {
                        "field": "economic_contribution_delta_rub",
                        "type": "quantitative",
                        "label": "Δ экономический вклад к факту, ₽",
                    },
                ],
            },
            "layout": "full",
            "palette": {"kind": "categorical"},
            "legend": {"position": "bottom", "sort": "spec"},
        },
        {
            "id": "scenario_comparison_chart",
            "title": "Сервис обычных продаж: факт и ключевые варианты",
            "subtitle": "Доля записанных продаж за февраль–июль 2026 года.",
            "showDescription": True,
            "intent": "comparison",
            "question": "Насколько защита приблизила модель к фактическому запасу?",
            "rationale": "Пять столбцов показывают абсолютный сервис без перегрузки техническими сценариями.",
            "comparisonContext": {
                "baseline": "Фактический запас",
                "grain": "Ключевой сценарий",
                "unit": "fraction",
            },
            "type": "bar",
            "dataset": "scenario_comparison",
            "sourceId": "scenario_summary",
            "encodings": {
                "x": {
                    "field": "scenario_label",
                    "type": "nominal",
                    "label": "Вариант",
                },
                "y": {
                    "field": "observed_fill_rate",
                    "type": "quantitative",
                    "label": "Сервис обычных продаж",
                },
                "tooltip": [
                    {
                        "field": "gross_profit_rub",
                        "type": "quantitative",
                        "label": "Валовая прибыль, ₽",
                    },
                    {
                        "field": "average_inventory_value_rub",
                        "type": "quantitative",
                        "label": "Средний капитал, ₽",
                    },
                ],
            },
            "valueFormat": "percent",
            "layout": "full",
            "palette": {"kind": "categorical"},
        },
    ]
    tables: list[dict[str, Any]] = []
    report_markdown = build_markdown(analysis)
    executive_summary = (
        report_markdown.split("## Executive Summary", 1)[1]
        .split("## Что именно проверяли", 1)[0]
        .strip()
    )
    blocks = [
        {"id": "title", "type": "markdown", "body": f"# {title}"},
        {
            "id": "executive_summary",
            "type": "markdown",
            "body": f"## Executive Summary\n\n{executive_summary}",
            "sourceId": "frozen_summary",
        },
        {
            "id": "headline_metrics",
            "type": "metric-strip",
            "cardIds": [row["id"] for row in cards],
        },
        {
            "id": "scope",
            "type": "markdown",
            "body": (
                "## Что именно проверяли\n\n"
                f"Период — с 1 февраля по 31 июля 2026 года, когорта — {analysis['cohort']['sku_count']} SKU предмета «Дисплеи». Стадия на каждую дату восстановлена только по продажам. Сравнивались прежняя модель, центральный вариант `20% + P90 + 4 недели` и ещё 17 сочетаний защиты «Растим»."
            ),
            "sourceId": "preflight_manifest",
        },
        {
            "id": "result_finding",
            "type": "markdown",
            "body": (
                "## Защита вернула часть продаж, но не приблизила модель к факту достаточно\n\n"
                f"**Центральный вариант поднял сервис обычных продаж на {_pct(headline['observed_fill_improvement_vs_control'], 3)} и вернул {_money_m(headline['gross_profit_improvement_vs_control_rub'])} валовой прибыли относительно прежней модели.** Для этого средний капитал вырос на {_money_m(headline['capital_increase_vs_control_rub'])}. До факта всё ещё не хватает {_money_m(-headline['gross_profit_delta_rub'])} прибыли."
            ),
            "sourceId": "frozen_summary",
        },
        {
            "id": "scenario_comparison_chart_block",
            "type": "chart",
            "chartId": "scenario_comparison_chart",
        },
        {
            "id": "tradeoff_finding",
            "type": "markdown",
            "body": (
                "## Все 18 вариантов покупают небольшой сервис дополнительным капиталом\n\n"
                f"**Лучший сервис дал только {_pct(headline['best_service_fill_improvement_vs_control'], 3)} к прежней модели.** Он вернул {_money_m(headline['best_service_profit_improvement_vs_control_rub'])} прибыли, но потребовал {_money_m(headline['best_service_capital_increase_vs_control_rub'])} дополнительного капитала. Ни одна точка не достигла строгого порога."
            ),
            "sourceId": "scenario_summary",
        },
        {
            "id": "protection_tradeoff_chart_block",
            "type": "chart",
            "chartId": "protection_tradeoff_chart",
        },
        {
            "id": "entry_coverage_finding",
            "type": "markdown",
            "body": (
                "## Защитный период покрывает только треть спроса «Растим»\n\n"
                f"**На SKU, вошедшие в «Растим» внутри тестового периода, пришлось {_pct(headline['entered_sale_demand_share'], 1)} продаж этой стадии.** Остальные товары уже были в «Растим» к 1 февраля, поэтому правило удержания после входа на большую часть оборота не действует."
            ),
            "sourceId": "preflight_daily",
        },
        {
            "id": "economic_cap_finding",
            "type": "markdown",
            "body": (
                "## Экономический лимит почти всегда сильнее P90/P95\n\n"
                f"**В {_pct(headline['economic_safety_binding_share'], 1)} применимых решений именно экономический расчёт ограничил страховой запас.** Поэтому повышение percentile почти не меняет заказ: сервисный уровень требует больше, но экономический фильтр обрезает количество раньше."
            ),
            "sourceId": "decision_detail",
        },
        {
            "id": "stage_finding",
            "type": "markdown",
            "body": (
                "## Основной дефицит всё ещё находится в «Растим»\n\n"
                f"**На стадию «Растим» приходится {_pct(headline['sale_extra_lost_share'], 1)} дополнительного дефицита центрального варианта.** Защита вернула часть продаж, но исходный min/max для широкой группы ходовых SKU остаётся слишком низким."
            ),
            "sourceId": "stage_summary",
        },
        {"id": "stage_chart_block", "type": "chart", "chartId": "stage_loss_chart"},
        {
            "id": "time_finding",
            "type": "markdown",
            "body": (
                "## Разрыв снова накапливается к лету\n\n"
                "**Даже с новой защитой модель не удерживает фактический сервис по мере прохождения периода.** Месячный разрез показывает накопительный характер ошибки, а не единичный провал запуска новинок."
            ),
            "sourceId": "daily_summary",
        },
        {
            "id": "monthly_chart_block",
            "type": "chart",
            "chartId": "monthly_service_chart",
        },
        {
            "id": "manual_review_finding",
            "type": "markdown",
            "body": (
                "## Ручная очередь больше не размножает карточки ежедневно\n\n"
                f"Центральный сценарий создал {headline['manual_review_created']} карточек `manual_review`; ещё {headline['manual_review_updated']} событий обновили уже существующие карточки SKU. Это на {_pct(headline['manual_review_creation_reduction'], 1)} меньше новых карточек, чем при создании отдельной карточки на каждую ручную строку заказа."
            ),
            "sourceId": "frozen_summary",
        },
        {
            "id": "recommendations",
            "type": "markdown",
            "body": (
                "## Что делать дальше\n\n"
                "1. **Не включать автоматические заказы:** оставить только расчёт рекомендаций без отправки заказов.\n"
                "2. **Дать сервисный минимум всем SKU стадии «Растим», а не только недавно вошедшим.**\n"
                "3. **Развести сервис и экономический фильтр:** проверить P90/P95 как обязательный уровень, а экономику показывать отдельным предупреждением.\n"
                "4. **Проверить надёжность товара в пути:** отдельным сценарием учитывать опоздание или недопоставку pipeline.\n"
                "5. **Обновлять manual_review только при существенном изменении** причины или количества.\n"
                "6. **Forward shadow пока не запускать:** сначала новый frozen-backtest должен пройти прежний критерий."
            ),
        },
        {
            "id": "further_questions",
            "type": "markdown",
            "body": (
                "## Открытые вопросы\n\n"
                "- Готовы ли мы проверить обязательный P90/P95 без текущего экономического обрезания?\n"
                "- Включать ли в цену дефицита риск ухода клиента и срочной закупки?\n"
                "- Как учитывать замену одного качества/версии дисплея другим?\n"
                "- Какой порог изменения количества должен обновлять manual_review?"
            ),
        },
        {
            "id": "caveats",
            "type": "markdown",
            "body": (
                "## Ограничения\n\n"
                "- Фактический сервис обычных продаж равен 100% по определению: видны только состоявшиеся продажи; скрытый спрос измеряется отдельно.\n"
                "- Сайт связан с SKU только по валидному PRODUCT_XML_ID; строки вне когорты не распределялись догадкой.\n"
                f"- Предварительная проверка данных пройдена, но найдены {analysis['source_quality']['negative_register_balances']['value']} отрицательных дневных резервов и {analysis['source_quality']['unit_economics_coverage']['value']} строк решений без себестоимости.\n"
                "- Все положительные ручные рекомендации считаются принятыми; без этого реальный сервис будет ниже.\n"
                "- Production-заказы и внешние записи не создавались."
            ),
            "sourceId": "preflight_manifest",
        },
    ]
    charts = [chart for chart in charts if chart["id"] == "stage_loss_chart"]
    blocks = [
        block
        for block in blocks
        if "chartId" not in block or block["chartId"] == "stage_loss_chart"
    ]
    return {
        "surface": "report",
        "manifest": {
            "version": 1,
            "surface": "report",
            "title": title,
            "description": "Партнёрский отчёт о следующей стадийной модели автозаказа дисплеев.",
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
            "status": "ready",
            "generatedAt": analysis["generated_at"],
            "datasets": {
                "headline": headline_dataset,
                "stages": stages,
                "monthly_service": analysis["monthly_chart"],
                "protection_scenarios": scenario_base,
                "scenario_comparison": analysis["scenario_comparison"],
                "factor_effects": analysis["factor_effects"],
            },
            "accessIssues": [],
        },
        "sources": sources,
        "package_info": {
            "originUrl": "artifact://display-auto-order-grow-protection-frozen-backtest-2026-h1"
        },
    }


def build_source_notes(analysis: Mapping[str, Any]) -> str:
    return f"""# Примечания к источникам frozen-отчёта

## Аудитория и структура

- Аудитория: партнёр и владельцы бизнес-процесса.
- Структура: заголовок; краткий вывод; выводы с графиками; рекомендации;
  открытые вопросы; ограничения.
- Формат: portable HTML как статический источник для PDF.

## Карта графиков

| Раздел | Вопрос | Тип | Данные | Подтверждаемый вывод |
| --- | --- | --- | --- | --- |
| Ключевые варианты | Насколько защита приблизила модель к факту? | Столбцы по категориям | `frozen-scenario-summary.csv` | защита возвращает часть сервиса, но оставляет большой разрыв |
| Обмен сервиса на капитал | Что дали все 18 сочетаний? | Scatter, одна точка на сценарий | `frozen-scenario-summary.csv` | все варианты требуют больше капитала ради небольшого прироста сервиса |
| Исторические стадии | Где создаётся дополнительный дефицит? | Столбцы по категориям | `frozen-baseline-stage.csv` | `sale / Растим` доминирует в разрыве |
| Сервис по месяцам | Когда накапливается разрыв? | Сгруппированные месячные столбцы | `frozen-baseline-daily.csv` | разрыв расширяется к июню–июлю |

## Ограниченные разрезы

- Причинный вклад надёжности pipeline не показан отдельным графиком: текущий
  frozen-backtest не содержит сценарного haircut подтверждённого товара в пути.
- Средние эффекты уровней `10/20/30%`, `P75/P90/P95` и `2/4/6 недель`
  рассчитаны по сбалансированному неполному факторному плану и не трактуются как
  независимая причинная оценка каждого параметра.
- События сайта входят только при однозначной связи валидного `PRODUCT_XML_ID` с
  SKU когорты; события вне когорты не распределяются догадкой.
- `reserve_backlog` реализован и проверен, но в историческом периоде фактических
  случаев резерва сверх физического остатка не найдено.
- Отчёт использует classification run `{analysis['cohort']['classification_run_id']}`
  и SHA-256 frozen preflight manifest из `frozen-summary.json`.
"""


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight-dir", type=Path, required=True)
    parser.add_argument("--backtest-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    analysis = build_analysis(args.preflight_dir, args.backtest_dir)
    (args.backtest_dir / "frozen-report-analysis.json").write_text(
        json.dumps(analysis, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (args.backtest_dir / "FROZEN-BACKTEST-DIAGNOSTIC.md").write_text(
        build_markdown(analysis), encoding="utf-8"
    )
    (args.backtest_dir / "FROZEN-REPORT-SOURCE-NOTES.md").write_text(
        build_source_notes(analysis), encoding="utf-8"
    )
    artifact = build_artifact(analysis)
    (args.backtest_dir / "artifact.json").write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "status": "ready",
                "analysis": str(args.backtest_dir / "frozen-report-analysis.json"),
                "markdown": str(args.backtest_dir / "FROZEN-BACKTEST-DIAGNOSTIC.md"),
                "source_notes": str(args.backtest_dir / "FROZEN-REPORT-SOURCE-NOTES.md"),
                "artifact": str(args.backtest_dir / "artifact.json"),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
