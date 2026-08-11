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

BASE_SCENARIO_ID = "typical_kmp0_5_sitebalanced_base"
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
        if (
            row["holding_cost_scenario"] == "base"
            and row["stage_profile"] != "legacy"
            and (row.get("site_profile") or "off") == "balanced"
        ):
            scenario_chart.append(item)

    site_sensitivity = [
        row
        for row in scenarios
        if row["stage_profile"] == "typical"
        and row["kmp4_weight"] == 0.5
        and row["holding_cost_scenario"] == "base"
    ]
    site_off = next((row for row in site_sensitivity if row["site_profile"] == "off"), None)
    if site_off:
        for row in site_sensitivity:
            row["observed_fill_delta_vs_site_off"] = (
                row["observed_fill_rate"] - site_off["observed_fill_rate"]
            )
            row["gross_profit_delta_vs_site_off_rub"] = (
                row["gross_profit_delta_rub"] - site_off["gross_profit_delta_rub"]
            )
            row["capital_delta_vs_site_off_rub"] = (
                row["capital_delta_rub"] - site_off["capital_delta_rub"]
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
        "schema": "display_auto_order_frozen_report_analysis.v2",
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
            "hidden_kmp4_qty": _float(model.get("hidden_kmp4_qty")),
            "hidden_site_order_qty": _float(model.get("hidden_site_order_qty")),
            "hidden_site_cart_qty": _float(model.get("hidden_site_cart_qty")),
            "hidden_reserve_backlog_qty": _float(model.get("hidden_reserve_backlog_qty")),
        },
        "stages": stages,
        "monthly": monthly,
        "monthly_chart": monthly_chart,
        "scenarios": scenarios,
        "scenario_chart": scenario_chart,
        "site_sensitivity": site_sensitivity,
        "period_sensitivity": period_rows,
        "top_skus": top_skus,
        "trigger_counts": dict(trigger_counts),
        "source_quality": quality,
        "site_export": preflight.get("site_export", {}),
        "method": frozen["method"],
    }


def build_markdown(analysis: Mapping[str, Any]) -> str:
    headline = analysis["headline"]
    model = analysis["model"]
    actual = analysis["actual"]
    quality = analysis["source_quality"]
    passed = bool(analysis["acceptance"]["passed"])
    outcome = (
        "Модель прошла строгий критерий backtest, но ещё не разрешена для рабочих заказов."
        if passed
        else "Модель пока не прошла строгий критерий и не может создавать рабочие заказы."
    )
    site_rows = {row["site_profile"]: row for row in analysis["site_sensitivity"]}
    balanced_site = site_rows.get("balanced", {})
    periods: dict[str, dict[str, Mapping[str, Any]]] = defaultdict(dict)
    for row in analysis["period_sensitivity"]:
        periods[row["period"]][row["strategy"]] = row
    pre_july_model = periods.get("pre_july", {}).get("model", {})
    july_model = periods.get("july", {}).get("model", {})
    site_mapping = analysis.get("site_export", {}).get("mapping_stats", {})
    return f"""# Автозаказ дисплеев: почему модель не прошла проверку на истории

## Краткий вывод

- **{outcome}**
- Базовый вариант обслужил **{_pct(model['observed_fill_rate'])}** записанных продаж против **{_pct(actual['observed_fill_rate'])}** у фактического запаса.
- Разница валовой прибыли — **{_money_m(headline['gross_profit_delta_rub'])}**, разница среднего капитала — **{_money_m(headline['capital_delta_rub'])}**.
- **Главный провал — стадия «Растим».** На неё приходится {_pct(headline['sale_extra_lost_share'], 1)} дополнительного дефицита. Стартовый профиль новинок влияет на итог лишь на десятые доли процента.
- **Ручная нагрузка слишком велика.** {_pct(headline['manual_order_share'], 1)} строк заказа в базовом сценарии требуют принятого вручную внепланового решения.

## Что именно проверяли

Период — с 1 февраля по 31 июля 2026 года, когорта — {analysis['cohort']['sku_count']} SKU предмета «Дисплеи». На каждую дату стадия восстановлена только по продажам. Сайт, КМП4 и дефицитный резерв не переводят товар между стадиями, а лишь уточняют потребность и запускают внеплановый пересмотр.

Базовый сценарий: типичный стартовый профиль + КМП4 0,5 + сайт `balanced` (заказ ×1, дефицитная корзина ×0,25) + базовая стоимость запаса. В общей 14-дневной очереди одна продажа или один прирост резерва может погасить только один ранее созданный сигнал.

## Что добавил сайт

В frozen-файл вошло {site_mapping.get('mapped_row_count', '—')} сопоставленных строк сайта. После погашения и истечения очереди базовый сценарий увидел {headline['hidden_site_order_qty']:.0f} единиц скрытого спроса из заказов и {headline['hidden_site_cart_qty']:.0f} из дефицитных корзин. По сравнению с выключенным сайтом профиль `balanced` изменил сервис записанных продаж на {_pct(balanced_site.get('observed_fill_delta_vs_site_off', 0), 3)} и валовую прибыль на {_money_m(balanced_site.get('gross_profit_delta_vs_site_off_rub', 0))}.

Корзина учитывается количественно только при свободном остатке не выше нуля; повторные строки одной сессии по SKU за день сжимаются до одной единицы. `DELAY=Y` остаётся только ручным сигналом.

## Что исправлено в резерве

Отрицательный баланс регистра больше не создаёт виртуальный товар. В расчёте отдельно хранятся сырой резерв, эффективный резерв `max(raw, 0)` и дефицитный backlog сверх физического остатка. Рост backlog считается подтверждённой неудовлетворённой потребностью; его снижение с продажей — погашением, без продажи — отменой. За период скрытый спрос этого источника составил {headline['hidden_reserve_backlog_qty']:.0f} единиц.

## Почему модель не угадала

Модель больше не раздувает запас КМП4 и действительно высвобождает капитал. Но она слишком сильно экономит на товарах стадии «Растим»: там возникает {headline['sale_extra_lost_qty']:.0f} из {headline['extra_lost_total_qty']:.0f} дополнительных потерянных единиц. Провал усиливается к июню–июлю, когда скорость отдельных SKU меняется быстрее, чем накопленная история успевает перестроить запас.

Экономический safety stock по прошлым ошибкам прогноза и ежедневный контроль точки заказа улучшили сервис, но не закрыли разрыв. Это означает, что проблема системная: текущая формула распределяет слишком мало капитала в ходовые SKU, а не просто ошибается на нескольких новинках. Топ-10 SKU дают только {_pct(headline['top10_negative_gp_share'], 1)} отрицательной валовой прибыли, поэтому точечная правка списка товаров проблему не решит.

## До июля и июль отдельно

- До июля сервис модели: **{_pct(pre_july_model.get('fill_rate', 0))}**, валовая прибыль: **{_money_m(pre_july_model.get('gross_profit_rub', 0))}**.
- За июль сервис модели: **{_pct(july_model.get('fill_rate', 0))}**, валовая прибыль: **{_money_m(july_model.get('gross_profit_rub', 0))}**.

Июльский скачок корзин не удалён из расчёта. Он показан отдельно, чтобы партнёр видел, насколько итог чувствителен к изменению структуры сайта.

## Что делать дальше

1. **Не включать автоматические заказы.** Оставить только расчёт рекомендаций без отправки заказов.
2. **Ввести отдельную сервисную защиту для «Растим».** Калибровать её по стоимости дефицита и эмпирической ошибке прогноза, а не единым числом дней для всех SKU.
3. **Сократить очередь ручных решений.** Один активный сигнал на SKU должен обновляться до закрытия, а не создавать новую строку при каждом дне ускорения.
4. **Провести forward shadow без заказов.** Даже успешная история не доказывает, что сигналы сайта и резерва будут приходить вовремя в текущем процессе.
5. **Сохранить раздельный мониторинг источников.** Заказы сайта, корзины, КМП4 и backlog нельзя сливать в один непрозрачный коэффициент.

## Открытые вопросы

- Какой минимальный уровень наличия утверждаем отдельно для стадии «Растим» и дорогих дисплеев?
- Как оценивать замену одного качества/версии дисплея другим, чтобы не считать весь дефицит потерянной продажей?
- Какой объём ежедневной очереди `manual_review` реально может обработать закупщик?

## Ограничения

- Фактический fill rate обычных продаж равен 100% по определению: видны только состоявшиеся продажи. Незарегистрированный спрос оценивается через несколько косвенных источников.
- Сайт сопоставляется только по валидному `PRODUCT_XML_ID`; строки вне когорты не распределяются догадкой.
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
    title = "Автозаказ дисплеев: почему модель не прошла проверку на истории"
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
            "next-stage-model-preflight-site-reserve-v2/run-manifest.json",
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
    site_sensitivity = sorted(analysis["site_sensitivity"], key=lambda row: row["site_profile"])
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
        {
            "id": "site_profile_chart",
            "title": "Эффект профиля интернет-магазина",
            "subtitle": "Typical, КМП4 0,5 и базовая стоимость запаса.",
            "showDescription": True,
            "intent": "comparison",
            "question": "Как сайт меняет сервис обычных продаж?",
            "rationale": "Профили отличаются только весом заказа и дефицитной корзины.",
            "comparisonContext": {
                "baseline": "Сайт выключен",
                "grain": "Профиль сайта",
                "unit": "fraction",
            },
            "type": "bar",
            "dataset": "site_sensitivity",
            "sourceId": "scenario_summary",
            "encodings": {
                "x": {
                    "field": "site_profile_label",
                    "type": "nominal",
                    "label": "Профиль сайта",
                },
                "y": {
                    "field": "observed_fill_rate",
                    "type": "quantitative",
                    "label": "Сервис обычных продаж",
                },
                "tooltip": [
                    {
                        "field": "gross_profit_delta_vs_site_off_rub",
                        "type": "quantitative",
                        "label": "Δ прибыли к site off, ₽",
                    },
                    {
                        "field": "capital_delta_vs_site_off_rub",
                        "type": "quantitative",
                        "label": "Δ капитала к site off, ₽",
                    },
                ],
            },
            "valueFormat": "percent",
            "layout": "full",
            "palette": {"kind": "categorical"},
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
                f"Период — с 1 февраля по 31 июля 2026 года, когорта — {analysis['cohort']['sku_count']} SKU предмета «Дисплеи». Стадия на каждую дату восстановлена только по продажам. Базовый сценарий: typical + КМП4 0,5 + site balanced + cost base."
            ),
            "sourceId": "preflight_manifest",
        },
        {
            "id": "site_finding",
            "type": "markdown",
            "body": (
                "## Сайт и резерв теперь учтены отдельно\n\n"
                f"Заказы сайта дали {headline['hidden_site_order_qty']:.0f} единиц скрытого спроса, дефицитные корзины — {headline['hidden_site_cart_qty']:.0f}, backlog резерва — {headline['hidden_reserve_backlog_qty']:.0f}. Все источники проходят через общую 14-дневную очередь без двойного погашения одной продажей."
            ),
            "sourceId": "preflight_manifest",
        },
        {"id": "site_profile_chart_block", "type": "chart", "chartId": "site_profile_chart"},
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
                "3. КМП4, сайт и backlog проходят через одну очередь без двойного счёта.\n"
                "4. Отрицательный резерв больше не увеличивает доступный остаток.\n"
                "5. Корзина учитывается только при дефиците и с дневным cap=1.\n"
                "6. Июльский скачок сайта сохранён и показан отдельно."
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
                "4. **Провести forward shadow без заказов** с теми же критериями приёмки.\n"
                "5. **Мониторить источники раздельно:** КМП4, заказы сайта, корзины и backlog."
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
                "- Сайт связан с SKU только по валидному PRODUCT_XML_ID; строки вне когорты не распределялись догадкой.\n"
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
                "site_sensitivity": site_sensitivity,
            },
            "accessIssues": [],
        },
        "sources": sources,
        "package_info": {
            "originUrl": "artifact://display-auto-order-next-stage-frozen-backtest-2026-h1"
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
| Профили сайта | Как сигналы сайта меняют сервис? | Столбцы по категориям | `frozen-scenario-summary.csv` | сайт добавляет спрос, но не закрывает дефицит |
| Исторические стадии | Где создаётся дополнительный дефицит? | Столбцы по категориям | `frozen-baseline-stage.csv` | `sale / Растим` доминирует в разрыве |
| Сервис по месяцам | Когда накапливается разрыв? | Сгруппированные месячные столбцы | `frozen-baseline-daily.csv` | разрыв расширяется к июню–июлю |
| Чувствительность сценариев | Решают ли проблему профиль и вес КМП4? | Сгруппированные столбцы | `frozen-scenario-summary.csv` | ни один сценарий не проходит acceptance |

## Ограниченные разрезы

- Причинный вклад lead time не показан отдельным графиком: агрегированную потерю
  SKU нельзя надёжно отнести к одному меняющемуся дневному сроку поставки без
  более крупной SKU-day выгрузки.
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
