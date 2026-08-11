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

BASE_SCENARIO_ID = "typical_kmp0_5_base"
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
            "holding_cost_scenario": row["holding_cost_scenario"],
            "total_fill_rate": _float(row["fill_rate"]),
            "observed_fill_rate": _float(row["observed_fill_rate"]),
            "hidden_fill_rate": _float(row["hidden_fill_rate"]),
            "gross_profit_delta_rub": _float(row["gross_profit_delta_rub"]),
            "capital_delta_rub": _float(row["capital_delta_rub"]),
            "gmroi_annualized": _float(row["gmroi_annualized"]),
            "manual_order_share": (
                _float(row["manual_order_lines"]) / float(order_lines) if order_lines else 0.0
            ),
            "safety_stock_units_ordered": _float(row["safety_stock_units_ordered"]),
        }
        scenarios.append(item)
        if row["holding_cost_scenario"] == "base" and row["stage_profile"] != "legacy":
            scenario_chart.append(item)

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
    for row in _read_csv(backtest_dir / "frozen-baseline-decisions.csv"):
        if (
            row.get("scenario_id") == BASE_SCENARIO_ID
            and _decimal(row.get("recommended_order_qty")) > 0
        ):
            trigger_counts[row.get("decision_trigger") or "unknown"] += 1

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
        "schema": "display_auto_order_frozen_report_analysis.v1",
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "period": {"date_from": frozen["date_from"], "date_to": frozen["date_to"]},
        "cohort": {
            "sku_count": int(quality["source_count_cohort_sku_count"]["value"]),
            "classification_run_id": preflight["classification_run_id"],
            "decision_input_rows": preflight["row_counts"]["decision_inputs"],
        },
        "acceptance": frozen["acceptance"],
        "actual": actual,
        "model": model,
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
            "manual_order_share": float(manual_share),
            "extra_lost_total_qty": float(extra_lost_total),
            "sale_extra_lost_qty": float(sale_extra_lost),
            "sale_extra_lost_share": float(_ratio(sale_extra_lost, extra_lost_total)),
            "top10_negative_gp_share": float(
                Decimal(str(top10_loss)) / negative_gp_total if negative_gp_total else Decimal("0")
            ),
        },
        "stages": stages,
        "monthly": monthly,
        "monthly_chart": monthly_chart,
        "scenarios": scenarios,
        "scenario_chart": scenario_chart,
        "top_skus": top_skus,
        "trigger_counts": dict(trigger_counts),
        "source_quality": quality,
        "method": frozen["method"],
    }


def build_markdown(analysis: Mapping[str, Any]) -> str:
    headline = analysis["headline"]
    model = analysis["model"]
    actual = analysis["actual"]
    quality = analysis["source_quality"]
    return f"""# Новая модель автозаказа дисплеев — результат проверки на истории

## Краткий вывод

- **Модель пока нельзя включать в рабочую систему.** Она снизила бы средний складской капитал на **{_money_m(-_decimal(headline['capital_delta_rub']))}**, но одновременно потеряла бы **{_money_m(-_decimal(headline['gross_profit_delta_rub']))}** валовой прибыли.
- **Обычные продажи обслужены на {_pct(model['observed_fill_rate'])} против 100% записанных фактических продаж.** Для скрытого спроса КМП4 результат {_pct(model['hidden_fill_rate'])} против {_pct(actual['hidden_fill_rate'])} у фактического запаса.
- **Главный провал — стадия «Растим».** На неё приходится {_pct(headline['sale_extra_lost_share'], 1)} дополнительного дефицита. Стартовый профиль новинок влияет на итог лишь на десятые доли процента.
- **Ручная нагрузка слишком велика.** {_pct(headline['manual_order_share'], 1)} строк заказа в базовом сценарии требуют принятого вручную внепланового решения.

## Что именно проверяли

Период — с 1 февраля по 31 июля 2026 года, когорта — {analysis['cohort']['sku_count']} SKU предмета «Дисплеи». На каждую дату восстановлена историческая стадия SKU, остаток, резерв, свободный товар в пути, КМП4 и доступный на тот момент срок поставки. Базовый сценарий: типичный стартовый профиль, вес КМП4 0,5 и базовая стоимость хранения.

## Почему модель не угадала

Модель больше не раздувает запас КМП4 и действительно высвобождает капитал. Но она слишком сильно экономит на товарах стадии «Растим»: там возникает {headline['sale_extra_lost_qty']:.0f} из {headline['extra_lost_total_qty']:.0f} дополнительных потерянных единиц. Провал усиливается к июню–июлю, когда скорость отдельных SKU меняется быстрее, чем накопленная история успевает перестроить запас.

Экономический safety stock по прошлым ошибкам прогноза и ежедневный контроль точки заказа улучшили сервис, но не закрыли разрыв. Это означает, что проблема системная: текущая формула распределяет слишком мало капитала в ходовые SKU, а не просто ошибается на нескольких новинках. Топ-10 SKU дают только {_pct(headline['top10_negative_gp_share'], 1)} отрицательной валовой прибыли, поэтому точечная правка списка товаров проблему не решит.

## Что было исправлено в тесте

1. Внеплановые `manual_review` теперь участвуют в сценарии «все рекомендации приняты».
2. SKU, появившиеся внутри периода, получают свой реальный первый стартовый остаток как внешний запуск, а не как угаданный автозаказ.
3. Стартовый профиль новинки включается только после первого реального положительного остатка или прихода исходного стартового заказа.
4. КМП4 прибавляется к min/max один раз как очередь потребности, а не растягивается как ежедневная скорость на весь срок поставки.
5. P50 определяет ожидаемую дату прихода, P75 — только защитный горизонт покрытия.
6. Ускорение определяется по 30 и 90 дням, а ежедневная защита от дефицита поднимает внеплановый ручной пересмотр.
7. Защитный запас считается по завершённым прошлым ошибкам прогноза без подсматривания в будущее.

## Что делать дальше

1. **Не включать автоматические заказы.** Оставить только расчёт рекомендаций без отправки заказов.
2. **Ввести отдельную сервисную защиту для «Растим».** Калибровать её по стоимости дефицита и эмпирической ошибке прогноза, а не единым числом дней для всех SKU.
3. **Сократить очередь ручных решений.** Один активный сигнал на SKU должен обновляться до закрытия, а не создавать новую строку при каждом дне ускорения.
4. **Проверить SKU-связанные события интернет-магазина.** В этом backtest сайт не включён: используются продажи, резервы и КМП4.
5. **После настройки снова проверить модель на истории, затем на текущих данных без заказов.** Критерий прежний: прибыль и сервис не ниже факта, капитал ниже либо GMROI выше.

## Открытые вопросы

- Какой минимальный уровень наличия утверждаем отдельно для стадии «Растим» и дорогих дисплеев?
- Как оценивать замену одного качества/версии дисплея другим, чтобы не считать весь дефицит потерянной продажей?
- Какой объём ежедневной очереди `manual_review` реально может обработать закупщик?

## Ограничения

- Фактический fill rate обычных продаж равен 100% по определению: в данных видны только состоявшиеся продажи. Незарегистрированный спрос оценивается отдельно через КМП4.
- Исторические события сайта без надёжной связи с SKU не восстанавливались догадкой.
- Предварительная проверка данных пройдена, но найдены {quality['negative_register_balances']['value']} отрицательных дневных резервов и {quality['unit_economics_coverage']['value']} строк решений без себестоимости; они не скрыты и остаются ограничением.
- В сценарии все положительные ручные рекомендации считаются принятыми. Реальный результат без этой дисциплины будет хуже.
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
                "Базовый сценарий typical / КМП4 0,5 / base",
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
    title = "Новая модель автозаказа дисплеев — результат проверки на истории"
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
            "next-stage-model-preflight/run-manifest.json",
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
        }
    ]
    scenario_base = sorted(analysis["scenario_chart"], key=lambda row: row["scenario_id"])
    cards = [
        {
            "id": "observed_fill_card",
            "description": "Доля записанных продаж, которую смог бы обслужить запас модели.",
            "dataset": "headline",
            "sourceId": "frozen_summary",
            "metrics": [
                {
                    "label": "Сервис обычных продаж",
                    "field": "observed_fill_rate",
                    "format": "percent",
                },
                {
                    "label": "к факту",
                    "field": "observed_fill_delta",
                    "format": "percent",
                    "signed": True,
                },
            ],
        },
        {
            "id": "profit_delta_card",
            "description": "Разница валовой прибыли модели и фактической стратегии.",
            "dataset": "headline",
            "sourceId": "frozen_summary",
            "metrics": [
                {
                    "label": "Δ валовая прибыль, млн ₽",
                    "field": "gross_profit_delta_million_rub",
                    "format": "number",
                    "signed": True,
                }
            ],
        },
        {
            "id": "capital_delta_card",
            "description": "Изменение средней стоимости складского остатка.",
            "dataset": "headline",
            "sourceId": "frozen_summary",
            "metrics": [
                {
                    "label": "Δ складской капитал, млн ₽",
                    "field": "capital_delta_million_rub",
                    "format": "number",
                    "signed": True,
                }
            ],
        },
        {
            "id": "manual_share_card",
            "description": "Доля строк заказа, для которых backtest предполагает ручное принятие.",
            "dataset": "headline",
            "sourceId": "frozen_summary",
            "metrics": [
                {
                    "label": "Ручные решения",
                    "field": "manual_order_share",
                    "format": "percent",
                }
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
            "id": "scenario_service_chart",
            "title": "Сервис обычных продаж в сценарной сетке",
            "subtitle": "Базовая стоимость хранения; профили запуска и веса КМП4.",
            "showDescription": True,
            "intent": "comparison",
            "question": "Решает ли проблему другой стартовый профиль или вес КМП4?",
            "rationale": "Сгруппированные столбцы сравнивают профили при одинаковом весе сигнала.",
            "comparisonContext": {
                "baseline": "Базовый сценарий typical / КМП4 0,5",
                "grain": "Сценарий",
                "unit": "fraction",
            },
            "type": "bar",
            "dataset": "scenario_base_cost",
            "sourceId": "scenario_summary",
            "encodings": {
                "x": {
                    "field": "kmp4_label",
                    "type": "ordinal",
                    "label": "Вес КМП4",
                },
                "y": {
                    "field": "observed_fill_rate",
                    "type": "quantitative",
                    "label": "Сервис обычных продаж",
                },
                "color": {
                    "field": "profile_label",
                    "type": "nominal",
                    "label": "Стартовый профиль",
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
            "valueFormat": "percent",
            "layout": "full",
            "palette": {"kind": "categorical"},
            "legend": {"position": "bottom", "sort": "spec"},
            "settings": {"groupMode": "grouped"},
        },
    ]
    tables: list[dict[str, Any]] = []
    summary_body = build_markdown(analysis).split("## Что именно проверяли", 1)[0]
    blocks = [
        {"id": "title", "type": "markdown", "body": f"# {title}"},
        {
            "id": "executive_summary",
            "type": "markdown",
            "body": summary_body.split("\n", 2)[2].strip(),
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
                f"Период — с 1 февраля по 31 июля 2026 года, когорта — {analysis['cohort']['sku_count']} SKU предмета «Дисплеи». На каждую дату восстановлены историческая стадия, остаток, резерв, свободный товар в пути, КМП4 и доступный на тот момент срок поставки. Базовый сценарий: типичный стартовый профиль, вес КМП4 0,5 и базовая стоимость хранения."
            ),
            "sourceId": "preflight_manifest",
        },
        {
            "id": "stage_finding",
            "type": "markdown",
            "body": (
                "## Главный провал — стадия «Растим»\n\n"
                f"**На «Растим» приходится {_pct(headline['sale_extra_lost_share'], 1)} дополнительного дефицита.** Модель высвобождает капитал именно из ходовых SKU быстрее, чем сокращает их потребность. Поэтому экономия склада превращается в потерю продаж и валовой прибыли."
            ),
            "sourceId": "stage_summary",
        },
        {"id": "stage_chart_block", "type": "chart", "chartId": "stage_loss_chart"},
        {
            "id": "time_finding",
            "type": "markdown",
            "body": (
                "## Разрыв накапливается к лету\n\n"
                "**В феврале модель почти совпадает с фактом, но к июню–июлю сервис обычных продаж падает примерно до 89%.** Это признак накопленной недооценки отдельных ходовых SKU, а не разовой ошибки стартового запаса."
            ),
            "sourceId": "daily_summary",
        },
        {
            "id": "monthly_chart_block",
            "type": "chart",
            "chartId": "monthly_service_chart",
        },
        {
            "id": "scenario_finding",
            "type": "markdown",
            "body": (
                "## Другой профиль новинок проблему не решает\n\n"
                "**Сервисный, типичный и осторожный стартовые профили отличаются лишь на десятые доли процента.** Вес КМП4 меняет состав измеряемого спроса и расход запаса, но ни один сценарий не выполняет одновременно условия по прибыли, сервису и капиталу."
            ),
            "sourceId": "scenario_summary",
        },
        {
            "id": "scenario_chart_block",
            "type": "chart",
            "chartId": "scenario_service_chart",
        },
        {
            "id": "concentration_finding",
            "type": "markdown",
            "body": (
                "## Потери распределены широко\n\n"
                f"**Топ-10 SKU объясняют только {_pct(headline['top10_negative_gp_share'], 1)} всей отрицательной валовой прибыли.** Исправить несколько карточек недостаточно: защита должна быть правилом для всей стадии «Растим» с учётом цены дефицита."
            ),
            "sourceId": "sku_summary",
        },
        {
            "id": "fixes",
            "type": "markdown",
            "body": (
                "## Что уже исправлено\n\n"
                "1. Внеплановые ручные решения включены в сценарий.\n"
                "2. Исторический запуск новых SKU больше не начинается с искусственного нуля.\n"
                "3. КМП4 считается разовой очередью потребности, а не ежедневной скоростью.\n"
                "4. P50 и P75 разделены на срок прихода и защитный горизонт.\n"
                "5. Рост спроса определяется по окнам 30/90 дней.\n"
                "6. Добавлены ежедневная защита от дефицита и защитный запас по прошлым ошибкам прогноза."
            ),
        },
        {
            "id": "recommendations",
            "type": "markdown",
            "body": (
                "## Что делать дальше\n\n"
                "1. **Не включать автоматические заказы:** оставить только расчёт рекомендаций без отправки заказов.\n"
                "2. **Откалибровать отдельную сервисную защиту «Растим»:** по эмпирической ошибке прогноза и стоимости дефицита, без единого числа дней для всех SKU.\n"
                "3. **Сократить ручную очередь:** один активный сигнал на SKU должен обновляться до закрытия, а не размножаться каждый день.\n"
                "4. **Проверить SKU-события сайта** и только после этого добавлять их как отдельный сигнал спроса.\n"
                "5. **Снова проверить модель на истории, затем на текущих данных без заказов** с теми же критериями приёмки."
            ),
        },
        {
            "id": "further_questions",
            "type": "markdown",
            "body": (
                "## Открытые вопросы\n\n"
                "- Какой минимальный уровень наличия утверждаем для «Растим» и дорогих дисплеев?\n"
                "- Как учитывать замену одного качества/версии дисплея другим?\n"
                "- Какой объём ежедневной очереди ручных решений реально может обработать закупщик?"
            ),
        },
        {
            "id": "caveats",
            "type": "markdown",
            "body": (
                "## Ограничения\n\n"
                "- Фактический сервис обычных продаж равен 100% по определению: видны только состоявшиеся продажи; скрытый спрос измеряется отдельно.\n"
                "- История сайта без надёжной связи с SKU не восстанавливалась догадкой.\n"
                f"- Предварительная проверка данных пройдена, но найдены {analysis['source_quality']['negative_register_balances']['value']} отрицательных дневных резервов и {analysis['source_quality']['unit_economics_coverage']['value']} строк решений без себестоимости.\n"
                "- Все положительные ручные рекомендации считаются принятыми; без этого реальный сервис будет ниже."
            ),
            "sourceId": "preflight_manifest",
        },
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
                "scenario_base_cost": scenario_base,
            },
            "accessIssues": [],
        },
        "sources": sources,
        "package_info": {
            "originUrl": "artifact://display-auto-order-next-stage-frozen-backtest-2026-h1"
        },
    }


def build_source_notes(analysis: Mapping[str, Any]) -> str:
    return f"""# Frozen report source notes

## Audience and structure

- Audience: product stakeholders / partner.
- Required structure: title; Executive Summary; findings with visual evidence; recommendations; further questions; caveats.
- Delivery mode: portable HTML used as the static source for PDF conversion.

## Chart map

| Segment | Question | Type | Dataset | Claim |
| --- | --- | --- | --- | --- |
| Historical stages | Where is extra shortage created? | Categorical bar | `frozen-baseline-stage.csv` | `sale / Растим` dominates the gap |
| Monthly service | When does the gap build? | Grouped monthly bars | `frozen-baseline-daily.csv` | the gap widens by June–July |
| Scenario sensitivity | Do profile and KMP4 weight solve it? | Grouped bars | `frozen-scenario-summary.csv` | no tested combination passes acceptance |

## Omitted or qualified cuts

- Lead-time attribution is not charted because per-SKU aggregate loss cannot be causally assigned to one changing daily lead-time bucket without a larger SKU-day output.
- Site demand is excluded because historical events are not yet reliably linked to SKU.
- The report uses classification run `{analysis['cohort']['classification_run_id']}` and the frozen preflight manifest hash stored in `frozen-summary.json`.
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
