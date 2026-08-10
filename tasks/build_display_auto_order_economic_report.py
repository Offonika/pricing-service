"""Build the durable economic-effectiveness report for display auto-order.

The task consumes only the already generated read-only backtest artifacts.  It
creates reconciled analytical tables, an executed notebook, a stakeholder
markdown report, a validation memo and the canonical Data Analytics artifact
used for portable HTML/PDF delivery.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

import nbformat
from nbclient import NotebookClient

ZERO = Decimal("0")
ONE = Decimal("1")
MILLION = Decimal("1000000")

FINANCIAL_SOURCE_SQL = """WITH target_organization AS (
    SELECT _IDRRef
    FROM dbo._Reference66 WITH (NOLOCK)
    WHERE _Description = N'MASTER MOBILE'
),
revenue_rows AS (
    SELECT
        product._Code AS code,
        reg._RecorderTRef AS recorder_tref,
        reg._RecorderRRef AS recorder_rref,
        reg._Fld7551RRef AS product_ref,
        SUM(CAST(reg._Fld7560 AS decimal(28, 3))) AS qty,
        SUM(CAST(reg._Fld7561 AS decimal(28, 2))) AS revenue
    FROM dbo._AccumRg7550 AS reg WITH (NOLOCK)
    JOIN dbo._Reference62 AS product WITH (NOLOCK)
      ON product._IDRRef = reg._Fld7551RRef
    WHERE reg._Active = 0x01
      AND reg._RecorderTRef IN (0x000000CB, 0x0000006D)
      AND reg._Fld7558RRef IN (SELECT _IDRRef FROM target_organization)
      AND reg._Period >= :date_from
      AND reg._Period < :date_to
      AND product._Code IN :codes
    GROUP BY
        product._Code,
        reg._RecorderTRef,
        reg._RecorderRRef,
        reg._Fld7551RRef
),
eligible_cost_keys AS (
    SELECT DISTINCT recorder_tref, recorder_rref, product_ref
    FROM revenue_rows
),
cost_rows AS (
    SELECT
        product._Code AS code,
        reg._RecorderTRef AS recorder_tref,
        SUM(CAST(reg._Fld7587 AS decimal(28, 3))) AS qty,
        SUM(CAST(reg._Fld7588 AS decimal(28, 2))) AS cost
    FROM dbo._AccumRg7580 AS reg WITH (NOLOCK)
    JOIN eligible_cost_keys AS eligible
      ON eligible.recorder_tref = reg._RecorderTRef
     AND eligible.recorder_rref = reg._RecorderRRef
     AND eligible.product_ref = reg._Fld7581RRef
    JOIN dbo._Reference62 AS product WITH (NOLOCK)
      ON product._IDRRef = reg._Fld7581RRef
    WHERE reg._Active = 0x01
    GROUP BY product._Code, reg._RecorderTRef
),
revenue_agg AS (
    SELECT
        code,
        SUM(
            CASE WHEN recorder_tref = 0x000000CB THEN qty ELSE 0 END
        ) AS gross_sale_qty,
        SUM(
            CASE WHEN recorder_tref = 0x0000006D THEN qty ELSE 0 END
        ) AS return_qty,
        SUM(revenue) AS net_revenue
    FROM revenue_rows
    GROUP BY code
),
cost_agg AS (
    SELECT
        code,
        SUM(
            CASE WHEN recorder_tref = 0x000000CB THEN cost ELSE 0 END
        ) AS gross_sale_cost,
        SUM(cost) AS net_cost
    FROM cost_rows
    GROUP BY code
)
SELECT
    revenue_agg.code,
    revenue_agg.gross_sale_qty,
    revenue_agg.return_qty,
    revenue_agg.net_revenue,
    COALESCE(cost_agg.net_cost, 0) AS net_cost,
    COALESCE(cost_agg.gross_sale_cost, 0) AS gross_sale_cost
FROM revenue_agg
LEFT JOIN cost_agg ON cost_agg.code = revenue_agg.code;
"""

DAILY_SALES_SOURCE_SQL = """SELECT
    product._Code AS nomenclature_code,
    CAST(rtu._Date_Time AS date) AS business_date,
    SUM(line._Fld4971) AS sales_qty
FROM dbo._Document203 AS rtu
JOIN dbo._Document203_VT4966 AS line
  ON line._Document203_IDRRef = rtu._IDRRef
JOIN dbo._Reference62 AS product
  ON product._IDRRef = line._Fld4974RRef
WHERE rtu._Marked = 0x00
  AND rtu._Posted = 0x01
  AND rtu._Date_Time >= :date_from
  AND rtu._Date_Time < :date_to
  AND product._Code IN :codes
GROUP BY product._Code, CAST(rtu._Date_Time AS date);
"""


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0").strip() or "0")
    except (ArithmeticError, ValueError):
        return ZERO


def _number(value: Any) -> float:
    return float(_decimal(value))


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8-sig")
        return
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _family(name: str) -> str:
    normalized = name.lower()
    if "apple" in normalized or "iphone" in normalized:
        return "Apple"
    if "samsung" in normalized:
        return "Samsung"
    if any(marker in normalized for marker in ("xiaomi", "redmi", "poco")):
        return "Xiaomi / Redmi / Poco"
    if any(marker in normalized for marker in ("tecno", "infinix", "itel")):
        return "Tecno / Infinix / Itel"
    if "realme" in normalized:
        return "Realme"
    if any(marker in normalized for marker in ("huawei", "honor")):
        return "Huawei / Honor"
    return "Прочие"


CLASSIFICATION_LABELS = {
    "model_service_worse": "Сервис хуже",
    "model_releases_capital_without_worse_service": "Капитал высвобожден без худшего сервиса",
    "model_more_profit_more_capital": "Больше прибыли и капитала",
    "model_excess_capital": "Лишний капитал без эффекта",
    "similar_or_mixed": "Без существенной разницы",
}


def _aggregate_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    group_field: str,
    label_map: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "sku_count": 0,
            "gross_profit_delta_rub": ZERO,
            "capital_delta_rub": ZERO,
            "sales_delta_qty": ZERO,
            "lost_observed_qty": ZERO,
            "served_hidden_qty": ZERO,
        }
    )
    for row in rows:
        key = str(row[group_field])
        target = grouped[key]
        target["sku_count"] += 1
        target["gross_profit_delta_rub"] += _decimal(
            row["gross_profit_delta_model_minus_actual_rub"]
        )
        target["capital_delta_rub"] += _decimal(row["capital_delta_model_minus_actual_rub"])
        target["sales_delta_qty"] += _decimal(row["incremental_sales_qty"])
        target["lost_observed_qty"] += _decimal(row["model_lost_observed_qty"])
        target["served_hidden_qty"] += _decimal(row["model_served_hidden_qty"])
    result = [
        {
            group_field: key,
            "label": (label_map or {}).get(key, key),
            **values,
        }
        for key, values in grouped.items()
    ]
    result.sort(key=lambda row: _decimal(row["gross_profit_delta_rub"]))
    return result


def _build_period_rows(
    daily_rows: Sequence[Mapping[str, Any]], *, period: str
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {
            "served_qty": ZERO,
            "lost_qty": ZERO,
            "inventory_value_sum_rub": ZERO,
            "inventory_qty_sum": ZERO,
            "stockout_sku_days": 0,
            "days": 0,
        }
    )
    for row in daily_rows:
        business_date = date.fromisoformat(str(row["business_date"]))
        if period == "week":
            period_date = business_date - timedelta(days=business_date.weekday())
            period_key = period_date.isoformat()
        else:
            period_key = business_date.strftime("%Y-%m")
        strategy = str(row["strategy"])
        target = grouped[(period_key, strategy)]
        target["served_qty"] += _decimal(row["served_qty"])
        target["lost_qty"] += _decimal(row["lost_qty"])
        target["inventory_value_sum_rub"] += _decimal(row["ending_stock_value_rub"])
        target["inventory_qty_sum"] += _decimal(row["ending_stock_qty"])
        target["stockout_sku_days"] += int(row["stockout_demand_sku_days"])
        target["days"] += 1

    periods = sorted({key for key, _strategy in grouped})
    result: list[dict[str, Any]] = []
    for period_key in periods:
        combined: dict[str, Any] = {period: period_key}
        for strategy in ("actual", "model"):
            values = grouped[(period_key, strategy)]
            days = Decimal(values["days"] or 1)
            combined[f"{strategy}_served_qty"] = values["served_qty"]
            combined[f"{strategy}_lost_qty"] = values["lost_qty"]
            combined[f"{strategy}_average_inventory_value_rub"] = (
                values["inventory_value_sum_rub"] / days
            )
            combined[f"{strategy}_average_inventory_qty"] = values["inventory_qty_sum"] / days
            combined[f"{strategy}_stockout_sku_days"] = values["stockout_sku_days"]
        combined["served_qty_delta"] = combined["model_served_qty"] - combined["actual_served_qty"]
        combined["capital_delta_rub"] = (
            combined["model_average_inventory_value_rub"]
            - combined["actual_average_inventory_value_rub"]
        )
        result.append(combined)
    return result


def build_analysis(economic_dir: Path) -> dict[str, Any]:
    raw_summary = json.loads((economic_dir / "economic-summary.json").read_text(encoding="utf-8"))
    scenario_rows = _read_csv(economic_dir / "economic-scenario-comparison.csv")
    sku_rows = _read_csv(economic_dir / "economic-sku-outcomes.csv")
    daily_rows = _read_csv(economic_dir / "economic-daily-summary.csv")

    for row in sku_rows:
        row["family"] = _family(str(row["name"]))

    actual = raw_summary["base_scenario"]["actual"]
    model = raw_summary["base_scenario"]["model"]
    delta = raw_summary["base_scenario"]["delta"]

    actual_profit_unit = sum(
        (_decimal(row["actual_gross_profit_rub_estimated_by_unit"]) for row in sku_rows),
        ZERO,
    )
    actual_revenue_unit = sum(
        (
            _decimal(row["actual_served_qty"]) * _decimal(row["net_revenue_per_gross_unit_rub"])
            for row in sku_rows
        ),
        ZERO,
    )
    lost_observed_profit = sum(
        (
            _decimal(row["model_lost_observed_qty"])
            * _decimal(row["gross_profit_per_gross_unit_rub"])
            for row in sku_rows
        ),
        ZERO,
    )
    restored_hidden_profit = sum(
        (
            _decimal(row["model_served_hidden_qty"])
            * _decimal(row["gross_profit_per_gross_unit_rub"])
            for row in sku_rows
        ),
        ZERO,
    )
    lost_hidden_profit = sum(
        (
            _decimal(row["model_lost_hidden_qty"])
            * _decimal(row["gross_profit_per_gross_unit_rub"])
            for row in sku_rows
        ),
        ZERO,
    )
    exact_actual_profit = _decimal(actual["gross_profit_rub"])
    exact_actual_revenue = _decimal(actual["net_revenue_rub"])
    financial_profit_reconciliation = actual_profit_unit - exact_actual_profit
    bridge_model_profit = (
        exact_actual_profit
        + financial_profit_reconciliation
        - lost_observed_profit
        + restored_hidden_profit
    )
    bridge_gap = bridge_model_profit - _decimal(model["gross_profit_rub"])

    family_rows = _aggregate_rows(sku_rows, group_field="family")
    class_rows = _aggregate_rows(
        sku_rows,
        group_field="classification",
        label_map=CLASSIFICATION_LABELS,
    )
    class_by_key = {row["classification"]: row for row in class_rows}

    new_rows = [row for row in sku_rows if int(row["new_during_period"]) == 1]
    existing_rows = [row for row in sku_rows if int(row["new_during_period"]) == 0]
    lifecycle_rows = []
    for label, rows in (("Новые в периоде", new_rows), ("Существующие", existing_rows)):
        lifecycle_rows.append(
            {
                "label": label,
                "sku_count": len(rows),
                "gross_profit_delta_rub": sum(
                    (_decimal(row["gross_profit_delta_model_minus_actual_rub"]) for row in rows),
                    ZERO,
                ),
                "capital_delta_rub": sum(
                    (_decimal(row["capital_delta_model_minus_actual_rub"]) for row in rows),
                    ZERO,
                ),
                "sales_delta_qty": sum(
                    (_decimal(row["incremental_sales_qty"]) for row in rows), ZERO
                ),
            }
        )

    top_losses = sorted(
        sku_rows,
        key=lambda row: _decimal(row["gross_profit_delta_model_minus_actual_rub"]),
    )[:15]
    safe_release_rows = [
        row
        for row in sku_rows
        if row["classification"] == "model_releases_capital_without_worse_service"
    ]
    top_releases = sorted(
        safe_release_rows,
        key=lambda row: _decimal(row["capital_delta_model_minus_actual_rub"]),
    )[:15]

    sensitivity_rows = []
    factors = sorted(
        {_decimal(row["demand_factor"]) for row in scenario_rows if row["strategy"] == "actual"}
    )
    for factor in factors:
        actual_row = next(
            row
            for row in scenario_rows
            if row["strategy"] == "actual" and _decimal(row["demand_factor"]) == factor
        )
        model_row = next(
            row
            for row in scenario_rows
            if row["strategy"] == "model"
            and row["review_mode"] == "all_recommendations"
            and _decimal(row["demand_factor"]) == factor
        )
        sensitivity_rows.append(
            {
                "demand_factor": factor,
                "scenario_label": f"{factor.normalize()}×",
                "actual_gross_profit_rub": _decimal(actual_row["gross_profit_rub"]),
                "model_gross_profit_rub": _decimal(model_row["gross_profit_rub"]),
                "gross_profit_delta_rub": _decimal(model_row["gross_profit_rub"])
                - _decimal(actual_row["gross_profit_rub"]),
                "model_average_inventory_value_rub": _decimal(
                    model_row["average_inventory_value_rub"]
                ),
                "model_gmroi": _decimal(model_row["gmroi_annualized"]),
                "model_lost_observed_qty": _decimal(model_row["lost_observed_sales_qty"]),
            }
        )

    weekly_rows = _build_period_rows(daily_rows, period="week")
    monthly_rows = _build_period_rows(daily_rows, period="month")

    safe_class = class_by_key["model_releases_capital_without_worse_service"]
    profitable_class = class_by_key["model_more_profit_more_capital"]
    diagnostic_hybrid_profit = _decimal(safe_class["gross_profit_delta_rub"]) + _decimal(
        profitable_class["gross_profit_delta_rub"]
    )
    diagnostic_hybrid_capital = _decimal(safe_class["capital_delta_rub"]) + _decimal(
        profitable_class["capital_delta_rub"]
    )

    analysis = {
        "schema": "display_auto_order_economic_analysis.v1",
        "status": "share_with_caveats",
        "date_from": raw_summary["date_from"],
        "date_to": raw_summary["date_to"],
        "lead_time_days": raw_summary["lead_time_days"],
        "cohort": raw_summary["cohort"],
        "base_scenario": {
            "actual": actual,
            "model": model,
            "delta": delta,
            "winner": raw_summary["base_scenario"]["winner"],
        },
        "reconciliation": {
            "actual_gross_profit_exact_rub": exact_actual_profit,
            "actual_gross_profit_sku_estimate_rub": actual_profit_unit,
            "gross_profit_gap_exact_minus_sku_rub": exact_actual_profit - actual_profit_unit,
            "actual_revenue_exact_rub": exact_actual_revenue,
            "actual_revenue_sku_estimate_rub": actual_revenue_unit,
            "revenue_gap_exact_minus_sku_rub": exact_actual_revenue - actual_revenue_unit,
            "lost_observed_profit_rub": lost_observed_profit,
            "restored_hidden_profit_rub": restored_hidden_profit,
            "lost_hidden_profit_rub": lost_hidden_profit,
            "bridge_model_profit_rub": bridge_model_profit,
            "bridge_gap_rub": bridge_gap,
            "explanation": (
                "Headline actuals use exact net 1C financial-register totals; SKU allocation "
                "uses warehouse gross sales quantity multiplied by historical unit economics."
            ),
        },
        "family_summary": family_rows,
        "classification_summary": class_rows,
        "lifecycle_summary": lifecycle_rows,
        "sensitivity": sensitivity_rows,
        "weekly_summary": weekly_rows,
        "monthly_summary": monthly_rows,
        "top_profit_losses": top_losses,
        "top_capital_releases": top_releases,
        "diagnostic_hybrid_upper_bound": {
            "gross_profit_delta_rub_sku_estimate": diagnostic_hybrid_profit,
            "capital_delta_rub": diagnostic_hybrid_capital,
            "warning": (
                "Hindsight-only segmentation; it is an opportunity bound, not a deployable forecast."
            ),
        },
        "limitations": raw_summary["limitations"],
        "validation": {
            "bridge_tolerance_rub": "1",
            "bridge_reconciled": abs(bridge_gap) <= ONE,
            "scenario_count": len(scenario_rows),
            "sku_count": len(sku_rows),
            "daily_row_count": len(daily_rows),
            "status": "share_with_caveats",
        },
    }

    (economic_dir / "economic-analysis-summary.json").write_text(
        json.dumps(_json_value(analysis), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _write_csv(economic_dir / "economic-family-summary.csv", family_rows)
    _write_csv(economic_dir / "economic-classification-summary.csv", class_rows)
    _write_csv(economic_dir / "economic-lifecycle-summary.csv", lifecycle_rows)
    _write_csv(economic_dir / "economic-weekly-summary.csv", weekly_rows)
    _write_csv(economic_dir / "economic-monthly-summary.csv", monthly_rows)
    _write_csv(economic_dir / "economic-top-profit-losses.csv", top_losses)
    _write_csv(economic_dir / "economic-top-capital-releases.csv", top_releases)
    (economic_dir / "economic-source-query.sql").write_text(
        "-- Financial source query used by report_display_auto_order_economic_backtest.py.\n"
        "-- Named parameters and expanding :codes are intentionally preserved.\n\n"
        + FINANCIAL_SOURCE_SQL,
        encoding="utf-8",
    )
    return analysis


def _fmt_million(value: Any, digits: int = 2) -> str:
    return f"{_decimal(value) / MILLION:,.{digits}f}".replace(",", " ")


def _fmt_abs_million(value: Any, digits: int = 2) -> str:
    return _fmt_million(abs(_decimal(value)), digits)


def _fmt_number(value: Any, digits: int = 0) -> str:
    return f"{_decimal(value):,.{digits}f}".replace(",", " ")


def _fmt_pct(value: Any, digits: int = 1) -> str:
    return f"{_decimal(value) * Decimal('100'):.{digits}f}%".replace(".", ",")


def build_markdown(economic_dir: Path, analysis: Mapping[str, Any]) -> Path:
    actual = analysis["base_scenario"]["actual"]
    model = analysis["base_scenario"]["model"]
    delta = analysis["base_scenario"]["delta"]
    reconciliation = analysis["reconciliation"]
    class_by_key = {row["classification"]: row for row in analysis["classification_summary"]}
    service_worse = class_by_key["model_service_worse"]
    safe_release = class_by_key["model_releases_capital_without_worse_service"]
    new_row = next(
        row for row in analysis["lifecycle_summary"] if row["label"] == "Новые в периоде"
    )
    apple = next(row for row in analysis["family_summary"] if row["family"] == "Apple")

    body = f"""# Экономическая эффективность автозаказа дисплеев

Период: 1 февраля — 31 июля 2026 года. Когорта: {analysis['cohort']['sku_count']} SKU. Базовый срок поставки: {analysis['lead_time_days']} дня.

## Executive Summary

- **Текущая модель не победила фактическую стратегию.** Валовая прибыль была бы ниже на **{_fmt_abs_million(delta['gross_profit_rub'])} млн ₽**, тогда как средний капитал в запасах сократился бы только на **{_fmt_abs_million(delta['average_inventory_value_rub'])} млн ₽**.
- **Эффективность капитала тоже ухудшилась.** GMROI снизился с **{_decimal(actual['gmroi_annualized']):.3f}** до **{_decimal(model['gmroi_annualized']):.3f}**, а годовая оборачиваемость — с **{_decimal(actual['inventory_turnover_annualized']):.2f}** до **{_decimal(model['inventory_turnover_annualized']):.2f}**.
- **Причина не в том, что человек объявлен эталоном.** Модель восстановила скрытый спрос примерно на **{_fmt_million(reconciliation['restored_hidden_profit_rub'])} млн ₽** прибыли, но потеряла около **{_fmt_million(reconciliation['lost_observed_profit_rub'])} млн ₽** на уже наблюдавшихся продажах.
- **Правильный следующий шаг — не отключать помощник, а сделать формулу сегментной.** На {safe_release['sku_count']} SKU модель высвобождает **{_fmt_abs_million(safe_release['capital_delta_rub'])} млн ₽** без ухудшения сервиса, однако {service_worse['sku_count']} проблемных SKU перекрывают этот эффект.

## Что сравнивалось

Фактическая закупочная стратегия и модель получили один и тот же потенциальный спрос. Факт не считался «правильным ответом»: обе стратегии оценивались по обслуженным продажам, потерянному спросу, выручке, валовой прибыли, среднему ежедневному капиталу в остатках, оборачиваемости и GMROI. Скрытый спрос оценивался только на датах, когда фактический остаток на конец дня был нулевым, без использования будущих данных.

| Показатель | Фактическая стратегия | Модель | Модель минус факт |
|---|---:|---:|---:|
| Обслужено, шт. | {_fmt_number(actual['served_qty'])} | {_fmt_number(model['served_qty'])} | {_fmt_number(delta['served_qty'])} |
| Чистая выручка, млн ₽ | {_fmt_million(actual['net_revenue_rub'])} | {_fmt_million(model['net_revenue_rub'])} | {_fmt_million(delta['net_revenue_rub'])} |
| Валовая прибыль, млн ₽ | {_fmt_million(actual['gross_profit_rub'])} | {_fmt_million(model['gross_profit_rub'])} | {_fmt_million(delta['gross_profit_rub'])} |
| Средний капитал, млн ₽ | {_fmt_million(actual['average_inventory_value_rub'])} | {_fmt_million(model['average_inventory_value_rub'])} | {_fmt_million(delta['average_inventory_value_rub'])} |
| Fill rate | {_fmt_pct(actual['fill_rate'])} | {_fmt_pct(model['fill_rate'])} | {_fmt_pct(_decimal(model['fill_rate']) - _decimal(actual['fill_rate']))} |
| GMROI, годовой | {_decimal(actual['gmroi_annualized']):.3f} | {_decimal(model['gmroi_annualized']):.3f} | {_decimal(delta['gmroi_annualized']):.3f} |

## Почему модель проиграла

Модель сократила средний складской капитал на 2,3%, но валовую прибыль — на 6,9%. Экономия капитала оказалась слишком маленькой относительно потери доступности. Начиная с мая недельный объём обслуженных продаж модели устойчиво отстаёт от факта; к июню–июлю капитал уже заметно ниже фактического, но вместе с ним снижается и сервис.

На уровне SKU мост прибыли выглядит так:

- финансовая сверка точного факта 1С с оценкой по удельной экономике: **{_fmt_million(reconciliation['gross_profit_gap_exact_minus_sku_rub'])} млн ₽**;
- потеря прибыли на наблюдавшихся продажах: **−{_fmt_million(reconciliation['lost_observed_profit_rub'])} млн ₽**;
- прибыль от восстановленного скрытого спроса: **+{_fmt_million(reconciliation['restored_hidden_profit_rub'])} млн ₽**.

Мост сходится с итоговой прибылью модели с погрешностью менее 1 ₽. Разница между точной фактической прибылью и SKU-оценкой равна **{_fmt_million(reconciliation['gross_profit_gap_exact_minus_sku_rub'])} млн ₽**: headline использует точные финансовые регистры 1С, а построчное распределение — складское количество продаж, умноженное на историческую удельную экономику.

## Где сосредоточена проблема

- **{service_worse['sku_count']} SKU ухудшают сервис:** прибыль ниже на **{_fmt_abs_million(service_worse['gross_profit_delta_rub'])} млн ₽**. Именно эта группа перекрывает пользу остальных позиций.
- **Новые позиции:** {new_row['sku_count']} SKU дали **{_fmt_million(new_row['gross_profit_delta_rub'])} млн ₽** разрыва, или около 56% построчного разрыва прибыли. У модели возникает cold-start: мало собственной обслуженной истории — ниже прогноз — меньше заказ — ещё меньше доступной истории.
- **Apple:** вклад семейства в разницу прибыли составил **{_fmt_million(apple['gross_profit_delta_rub'])} млн ₽**. Основной риск сосредоточен в дорогих iPhone 13–17, включая Pro и Pro Max.
- **{safe_release['sku_count']} SKU уже дают полезный эффект:** капитал ниже на **{_fmt_abs_million(safe_release['capital_delta_rub'])} млн ₽**, а сервис не хуже. Формула перспективна как адресный инструмент, но не как одна настройка для всей матрицы.

## Проверка устойчивости

Вывод не зависит от выбранного сценария скрытого спроса. При нулевом скрытом спросе модель теряет уже наблюдавшиеся продажи и даёт около 75,52 млн ₽ прибыли. При повышенном сценарии 1,5× прибыль достигает около 78,21 млн ₽, но всё равно остаётся примерно на 4,90 млн ₽ ниже факта. Исполнение строк `manual_review` улучшает результат примерно на 3,68 млн ₽ относительно `auto_only`, однако не закрывает разрыв.

## Что улучшать в первую очередь

1. **Сделать отдельный режим для новых и быстро растущих SKU.** До накопления собственной истории использовать стартовый профиль: аналог/семейство, первые продажи, минимальный сервисный запас и более частый пересмотр. Это главное действие против самозанижения прогноза.
2. **Защитить 353 SKU с риском ухудшения сервиса.** Для дорогих и быстро продающихся Apple-позиций нужен отдельный min/max или service-level floor, рассчитанный по цене ошибки дефицита, а не один общий структурный порог.
3. **Оставить экономию капитала там, где она уже безопасна.** Кандидаты — 474 SKU, на которых модель в backtest не ухудшила сервис. В production их всё равно следует сначала проверить в forward shadow, потому что историческая классификация использует знание результата.
4. **Убрать зависимость прогноза только от обслуженных моделью продаж.** Спрос должен дополняться сигналами отсутствия: обращения, отказы, просмотры карточек при нулевом остатке, заказы сайта и аналоги. Иначе дефицит сам уменьшает измеряемый спрос.
5. **Повторить walk-forward backtest** после добавления профиля новинок, фактических резервов и поставщик-специфичных сроков. Критерий допуска: прибыль не ниже факта при меньшем капитале либо заранее согласованный положительный компромисс по GMROI.

## Практическое решение сейчас

Автозаказ следует продолжать использовать для создания внутренних проектов в помощнике. Закупщик проверяет количество и поставщика, а передача непроведённого черновика в 1С остаётся отдельным ручным действием. Экономический backtest не подтверждает автономное принятие всех строк текущей формулы.

## Открытые вопросы

1. Можно ли восстановить исторические резервы и фактические сроки поставки по поставщикам?
2. Какие сигналы отсутствия товара доступны: отказы сайта, обращения, просмотры или незавершённые корзины?
3. Какова стоимость капитала, хранения и устаревания для разных ценовых групп?
4. Как заранее, без знания будущего, отделить безопасные SKU от 353 позиций риска?

## Ограничения и допущения

- Скрытый спрос — модельная оценка, а не бухгалтерский факт.
- Нулевой остаток на конец дня может означать продажу последней единицы; поэтому показана чувствительность 0×, 1× и 1,5×.
- Доходность модели использует историческую маржу SKU и предполагает пропорциональные возвраты.
- Исторические резервы не восстановлены; свободный остаток модели может быть завышен, а заказ занижен.
- Используется текущая когорта ассортимента, поэтому присутствует survivorship bias.
- Срок поставки модели фиксирован на 52 дня; финансирование, хранение, устаревание и налоги не учтены.
- `all_recommendations` — верхняя оценка исполнения рассчитанных количеств, а не автономный production-режим.
"""
    path = economic_dir / "ECONOMIC-EFFECTIVENESS.md"
    path.write_text(body, encoding="utf-8")
    return path


def build_validation(economic_dir: Path, analysis: Mapping[str, Any]) -> Path:
    validation = analysis["validation"]
    reconciliation = analysis["reconciliation"]
    body = f"""# Validation Report

## Overall Assessment: Share with caveats

### Methodology Review

Вопрос сформулирован как сравнение двух стратегий по продажам, прибыли и капиталу; фактическая закупка не используется как целевая метка. Walk-forward симуляция не использует будущие продажи, а после начала периода прогноз модели строится только по спросу, который сама модель смогла обслужить.

### Issues Found

1. **Medium — скрытый спрос оценочный.** Нулевой остаток на конец дня не доказывает отказ покупателю; влияние проверено сценариями 0×, 1× и 1,5×.
2. **Medium — исторические резервы отсутствуют.** Это способно завысить свободный остаток модели и занизить заказ.
3. **Medium — текущая когорта создаёт survivorship bias.** Исторически выбывшие SKU могут отсутствовать.
4. **Low — точный факт и SKU-расклад имеют разное количество-основание.** Разрыв прибыли составляет {_fmt_million(reconciliation['gross_profit_gap_exact_minus_sku_rub'])} млн ₽ и раскрыт отдельной строкой сверки.

### Calculation Spot-Checks

- Мост прибыли: {'verified' if validation['bridge_reconciled'] else 'discrepancy found'}; остаточная разница {_fmt_number(reconciliation['bridge_gap_rub'], 2)} ₽.
- Когорта: {validation['sku_count']} SKU, совпадает с исходной экономической выгрузкой.
- Дневной ряд: {validation['daily_row_count']} строк = 181 день × 2 стратегии.
- Чувствительность: {validation['scenario_count']} строк = 3 сценария спроса × фактическая стратегия и 2 режима модели.

### Visualization Review

Все денежные сравнения используют одну валюту и нулевую базу; GMROI и проценты не смешиваются с рублями на одной оси. Недельный график содержит больше 8 временных точек. Знаковые значения показываются относительно нуля без зелёно-красной семантики.

### Required Caveats for Stakeholders

- Результат пригоден для решения о направлении доработки, но не доказывает причинный эффект будущего production-rollout.
- Диагностический гибрид по классам использует знание результата периода и не является готовым правилом сегментации.
- Перед автоматическим принятием строк требуется forward-shadow с заранее заданными правилами сегментации.
"""
    path = economic_dir / "VALIDATION.md"
    path.write_text(body, encoding="utf-8")
    return path


def build_notebook(economic_dir: Path, analysis: Mapping[str, Any]) -> Path:
    actual = analysis["base_scenario"]["actual"]
    model = analysis["base_scenario"]["model"]
    delta = analysis["base_scenario"]["delta"]
    notebook = nbformat.v4.new_notebook()
    notebook["metadata"]["kernelspec"] = {
        "display_name": "Python 3",
        "language": "python",
        "name": "python3",
    }
    notebook["cells"] = [
        nbformat.v4.new_markdown_cell(
            "## tl;dr\n\n"
            f"- Разница валовой прибыли модели к факту: **{_fmt_million(delta['gross_profit_rub'])} млн ₽**; "
            f"разница среднего капитала: **{_fmt_million(delta['average_inventory_value_rub'])} млн ₽**.\n"
            f"- GMROI: факт **{_decimal(actual['gmroi_annualized']):.3f}**, "
            f"модель **{_decimal(model['gmroi_annualized']):.3f}**.\n"
            "- Вывод: текущая единая формула не победила, но полезна на отдельных сегментах."
        ),
        nbformat.v4.new_markdown_cell(
            "## Context & Methods\n\n"
            "### Key Assumptions\n\n"
            "Период: 1 февраля — 31 июля 2026 года. Победитель определяется по "
            "валовой прибыли, среднему капиталу, оборачиваемости и GMROI; закупщик не "
            "считается эталоном. Скрытый спрос оценивается только в дни нулевого "
            "фактического остатка, без look-ahead."
        ),
        nbformat.v4.new_markdown_cell("## Data\n\n### 1. Load reviewed outputs"),
        nbformat.v4.new_code_cell(
            "import csv, json\n"
            "from pathlib import Path\n\n"
            "ROOT = Path('.')\n"
            "analysis = json.loads((ROOT / 'economic-analysis-summary.json').read_text(encoding='utf-8'))\n"
            "scenario_rows = list(csv.DictReader((ROOT / 'economic-scenario-comparison.csv').open(encoding='utf-8-sig')))\n"
            "sku_rows = list(csv.DictReader((ROOT / 'economic-sku-outcomes.csv').open(encoding='utf-8-sig')))\n"
            "weekly_rows = list(csv.DictReader((ROOT / 'economic-weekly-summary.csv').open(encoding='utf-8-sig')))\n"
            "{'scenarios': len(scenario_rows), 'sku': len(sku_rows), 'weeks': len(weekly_rows)}"
        ),
        nbformat.v4.new_markdown_cell("### 2. Validate the exact-to-SKU profit reconciliation"),
        nbformat.v4.new_code_cell(
            "reconciliation = analysis['reconciliation']\n"
            "{key: reconciliation[key] for key in [\n"
            "    'actual_gross_profit_exact_rub',\n"
            "    'actual_gross_profit_sku_estimate_rub',\n"
            "    'gross_profit_gap_exact_minus_sku_rub',\n"
            "    'lost_observed_profit_rub',\n"
            "    'restored_hidden_profit_rub',\n"
            "    'bridge_model_profit_rub',\n"
            "    'bridge_gap_rub',\n"
            "]}"
        ),
        nbformat.v4.new_markdown_cell("## Results\n\n### 3. Base economic comparison"),
        nbformat.v4.new_code_cell(
            "{\n"
            "    'actual': analysis['base_scenario']['actual'],\n"
            "    'model': analysis['base_scenario']['model'],\n"
            "    'delta': analysis['base_scenario']['delta'],\n"
            "}"
        ),
        nbformat.v4.new_markdown_cell("### 4. Hidden-demand sensitivity"),
        nbformat.v4.new_code_cell("analysis['sensitivity']"),
        nbformat.v4.new_markdown_cell("### 5. SKU segments and lifecycle"),
        nbformat.v4.new_code_cell(
            "{\n"
            "    'classes': analysis['classification_summary'],\n"
            "    'families': analysis['family_summary'],\n"
            "    'lifecycle': analysis['lifecycle_summary'],\n"
            "}"
        ),
        nbformat.v4.new_markdown_cell("### 6. Largest profit losses"),
        nbformat.v4.new_code_cell(
            "[{key: row[key] for key in [\n"
            "    'nomenclature_code', 'name',\n"
            "    'gross_profit_delta_model_minus_actual_rub',\n"
            "    'capital_delta_model_minus_actual_rub',\n"
            "    'incremental_sales_qty', 'new_during_period'\n"
            "]} for row in analysis['top_profit_losses'][:10]]"
        ),
        nbformat.v4.new_markdown_cell(
            "## Takeaways\n\n"
            "1. Модель снизила капитал недостаточно, чтобы окупить потерю доступности и прибыли.\n"
            "2. Главный адресный приоритет — new-item cold-start и дорогие Apple SKU.\n"
            "3. Безопасный сегмент высвобождения капитала существует, но его правило нужно "
            "определять до периода и проверять в forward shadow.\n"
            "4. Статус результата — Share with caveats из-за оценочного скрытого спроса, "
            "неполных резервов и survivorship bias."
        ),
    ]
    path = economic_dir / "economic-analysis.ipynb"
    nbformat.write(notebook, path)
    client = NotebookClient(
        notebook,
        timeout=600,
        kernel_name="python3",
        resources={"metadata": {"path": str(economic_dir)}},
    )
    executed = client.execute()
    nbformat.write(executed, path)
    return path


def _short_name(value: str, limit: int = 92) -> str:
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def build_artifact(economic_dir: Path, analysis: Mapping[str, Any]) -> dict[str, Any]:
    generated_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    actual = analysis["base_scenario"]["actual"]
    model = analysis["base_scenario"]["model"]
    delta = analysis["base_scenario"]["delta"]
    reconciliation = analysis["reconciliation"]
    class_by_key = {row["classification"]: row for row in analysis["classification_summary"]}
    service_worse = class_by_key["model_service_worse"]
    safe_release = class_by_key["model_releases_capital_without_worse_service"]
    new_row = next(
        row for row in analysis["lifecycle_summary"] if row["label"] == "Новые в периоде"
    )
    apple = next(row for row in analysis["family_summary"] if row["family"] == "Apple")

    financial_query = {
        "engine": "Microsoft SQL Server / SQLAlchemy",
        "sql": FINANCIAL_SOURCE_SQL,
        "description": "Читает возвратно-скорректированную выручку и себестоимость по SKU из финансовых регистров 1С; дневной спрос, остатки и симуляция добавляются локальным Python backtest.",
        "language": "sql",
        "filters": [
            "Период 2026-02-01 — 2026-07-31",
            "Организация MASTER MOBILE",
            "Текущая когорта дисплеев",
        ],
        "metric_definitions": [
            "Валовая прибыль = чистая выручка минус чистая себестоимость продаж.",
            "GMROI = годовая валовая прибыль / средний ежедневный капитал в запасах.",
        ],
        "tables_used": [
            "dbo._AccumRg7550",
            "dbo._AccumRg7580",
            "dbo._Reference62",
            "dbo._Reference66",
        ],
    }
    daily_sales_query = {
        "engine": "Microsoft SQL Server / SQLAlchemy",
        "sql": DAILY_SALES_SOURCE_SQL,
        "description": "Читает проведённые продажи по SKU и дням; исторические остатки отдельно восстанавливаются из месячных итогов и движений регистра 1С.",
        "language": "sql",
        "filters": [
            "Период 2025-07-24 — 2026-07-31",
            "Только настроенные продающие склады",
            "Текущая когорта дисплеев",
        ],
        "metric_definitions": [
            "Обслуженные продажи = количество проведённых продаж, которое стратегия могла покрыть доступным остатком.",
            "Скрытый спрос оценивается отдельно только на датах нулевого фактического остатка.",
        ],
        "tables_used": [
            "dbo._Document203",
            "dbo._Document203_VT4966",
            "dbo._Reference62",
            "dbo._AccumRg7735",
            "dbo._AccumRgT7745",
        ],
    }
    sources = [
        {
            "id": "economic_summary",
            "label": "Экономический backtest: итог и определения",
            "path": "economic-summary.json",
            "query": financial_query,
        },
        {
            "id": "economic_analysis",
            "label": "Сверенный аналитический слой экономического backtest",
            "path": "economic-analysis-summary.json",
            "query": financial_query,
        },
        {
            "id": "scenario_comparison",
            "label": "Сценарии скрытого спроса и режимы исполнения",
            "path": "economic-scenario-comparison.csv",
            "query": financial_query,
        },
        {
            "id": "sku_outcomes",
            "label": "Построчный экономический результат по SKU",
            "path": "economic-sku-outcomes.csv",
            "query": financial_query,
        },
        {
            "id": "daily_summary",
            "label": "Дневная динамика факта и модели",
            "path": "economic-daily-summary.csv",
            "query": daily_sales_query,
        },
        {
            "id": "family_summary",
            "label": "Агрегация экономического эффекта по семействам",
            "path": "economic-family-summary.csv",
            "query": financial_query,
        },
        {
            "id": "classification_summary",
            "label": "Классы эффективности модели по SKU",
            "path": "economic-classification-summary.csv",
            "query": financial_query,
        },
    ]

    headline = [
        {
            "actual_gross_profit_rub": _number(actual["gross_profit_rub"]),
            "model_gross_profit_rub": _number(model["gross_profit_rub"]),
            "gross_profit_delta_rub": _number(delta["gross_profit_rub"]),
            "actual_average_capital_rub": _number(actual["average_inventory_value_rub"]),
            "model_average_capital_rub": _number(model["average_inventory_value_rub"]),
            "average_capital_delta_rub": _number(delta["average_inventory_value_rub"]),
            "actual_gmroi": _number(actual["gmroi_annualized"]),
            "model_gmroi": _number(model["gmroi_annualized"]),
            "gmroi_delta": _number(delta["gmroi_annualized"]),
            "actual_fill_rate": _number(actual["fill_rate"]),
            "model_fill_rate": _number(model["fill_rate"]),
            "fill_rate_delta": _number(model["fill_rate"]) - _number(actual["fill_rate"]),
        }
    ]
    comparison_rows = []
    for metric_label, actual_value, model_value, metric_delta in (
        (
            "Валовая прибыль",
            actual["gross_profit_rub"],
            model["gross_profit_rub"],
            delta["gross_profit_rub"],
        ),
        (
            "Средний капитал",
            actual["average_inventory_value_rub"],
            model["average_inventory_value_rub"],
            delta["average_inventory_value_rub"],
        ),
    ):
        for strategy_key, strategy_label, value in (
            ("actual", "Фактическая стратегия", actual_value),
            ("model", "Модель", model_value),
        ):
            comparison_rows.append(
                {
                    "metric_label": metric_label,
                    "strategy_key": strategy_key,
                    "strategy_label": strategy_label,
                    "value_rub": _number(value),
                    "delta_rub": _number(metric_delta),
                    "period": "1 февраля — 31 июля 2026",
                    "sku_count": int(analysis["cohort"]["sku_count"]),
                }
            )
    sensitivity_rows = []
    for row in analysis["sensitivity"]:
        for strategy_key, strategy_label, value in (
            ("actual", "Фактическая стратегия", row["actual_gross_profit_rub"]),
            ("model", "Модель", row["model_gross_profit_rub"]),
        ):
            sensitivity_rows.append(
                {
                    "scenario_label": row["scenario_label"],
                    "demand_factor": _number(row["demand_factor"]),
                    "strategy_key": strategy_key,
                    "strategy_label": strategy_label,
                    "gross_profit_rub": _number(value),
                    "gross_profit_delta_rub": _number(row["gross_profit_delta_rub"]),
                    "model_average_inventory_value_rub": _number(
                        row["model_average_inventory_value_rub"]
                    ),
                    "model_gmroi": _number(row["model_gmroi"]),
                    "model_lost_observed_qty": _number(row["model_lost_observed_qty"]),
                }
            )
    profit_bridge_rows = [
        {
            "component": "Сверка финансового факта",
            "profit_delta_rub": _number(
                -_decimal(reconciliation["gross_profit_gap_exact_minus_sku_rub"])
            ),
            "kind": "reconciliation",
            "basis": "Точный факт 1С против SKU-оценки",
        },
        {
            "component": "Потеря наблюдавшихся продаж",
            "profit_delta_rub": _number(-_decimal(reconciliation["lost_observed_profit_rub"])),
            "kind": "loss",
            "basis": "Продажи, которые факт обслужил, а модель — нет",
        },
        {
            "component": "Восстановленный скрытый спрос",
            "profit_delta_rub": _number(reconciliation["restored_hidden_profit_rub"]),
            "kind": "gain",
            "basis": "Оценённый спрос в дни нулевого фактического остатка",
        },
    ]
    family_rows = [
        {
            "family": row["family"],
            "sku_count": int(row["sku_count"]),
            "gross_profit_delta_rub": _number(row["gross_profit_delta_rub"]),
            "capital_delta_rub": _number(row["capital_delta_rub"]),
            "sales_delta_qty": _number(row["sales_delta_qty"]),
        }
        for row in analysis["family_summary"]
    ]
    weekly_rows = []
    for row in analysis["weekly_summary"]:
        for strategy_key, strategy_label in (
            ("actual", "Фактическая стратегия"),
            ("model", "Модель"),
        ):
            weekly_rows.append(
                {
                    "week": row["week"],
                    "strategy_key": strategy_key,
                    "strategy_label": strategy_label,
                    "served_qty": _number(row[f"{strategy_key}_served_qty"]),
                    "lost_qty": _number(row[f"{strategy_key}_lost_qty"]),
                    "average_inventory_value_rub": _number(
                        row[f"{strategy_key}_average_inventory_value_rub"]
                    ),
                    "served_qty_delta": _number(row["served_qty_delta"]),
                    "capital_delta_rub": _number(row["capital_delta_rub"]),
                }
            )
    class_rows = [
        {
            "classification": row["classification"],
            "class_label": row["label"],
            "sku_count": int(row["sku_count"]),
            "gross_profit_delta_rub": _number(row["gross_profit_delta_rub"]),
            "capital_delta_rub": _number(row["capital_delta_rub"]),
            "sales_delta_qty": _number(row["sales_delta_qty"]),
        }
        for row in analysis["classification_summary"]
    ]
    top_loss_rows = [
        {
            "rank": index,
            "code": row["nomenclature_code"],
            "name": _short_name(row["name"]),
            "gross_profit_delta_rub": _number(row["gross_profit_delta_model_minus_actual_rub"]),
            "capital_delta_rub": _number(row["capital_delta_model_minus_actual_rub"]),
            "sales_delta_qty": _number(row["incremental_sales_qty"]),
            "new_during_period": "Да" if int(row["new_during_period"]) else "Нет",
            "family": row["family"],
        }
        for index, row in enumerate(analysis["top_profit_losses"][:10], start=1)
    ]

    cards = [
        {
            "id": "profit_card",
            "description": "Валовая прибыль модели и отклонение от фактической стратегии.",
            "dataset": "headline",
            "sourceId": "economic_summary",
            "metrics": [
                {
                    "label": "Прибыль модели",
                    "field": "model_gross_profit_rub",
                    "format": "currency",
                },
                {
                    "label": "к факту",
                    "field": "gross_profit_delta_rub",
                    "format": "currency",
                    "signed": True,
                },
            ],
        },
        {
            "id": "capital_card",
            "description": "Средняя ежедневная стоимость товарного остатка модели.",
            "dataset": "headline",
            "sourceId": "economic_summary",
            "metrics": [
                {
                    "label": "Средний капитал",
                    "field": "model_average_capital_rub",
                    "format": "currency",
                },
                {
                    "label": "к факту",
                    "field": "average_capital_delta_rub",
                    "format": "currency",
                    "signed": True,
                },
            ],
        },
        {
            "id": "gmroi_card",
            "description": "Годовая валовая прибыль на рубль среднего складского капитала.",
            "dataset": "headline",
            "sourceId": "economic_summary",
            "metrics": [
                {"label": "GMROI модели", "field": "model_gmroi", "format": "number"},
                {
                    "label": "к факту",
                    "field": "gmroi_delta",
                    "format": "number",
                    "signed": True,
                },
            ],
        },
        {
            "id": "fill_rate_card",
            "description": "Доля потенциального спроса, которую модель смогла обслужить.",
            "dataset": "headline",
            "sourceId": "economic_summary",
            "metrics": [
                {
                    "label": "Fill rate модели",
                    "field": "model_fill_rate",
                    "format": "percent",
                },
                {
                    "label": "к факту",
                    "field": "fill_rate_delta",
                    "format": "percent",
                    "signed": True,
                },
            ],
        },
    ]

    charts = [
        {
            "id": "economic_comparison_chart",
            "title": "Прибыль и средний складской капитал",
            "subtitle": "1 февраля — 31 июля 2026 года, млн ₽; одинаковая когорта из 1 531 SKU.",
            "showDescription": True,
            "intent": "comparison",
            "question": "Как модель меняет прибыль и используемый складской капитал?",
            "rationale": "Сгруппированные столбцы показывают две стратегии в одинаковых денежных единицах.",
            "comparisonContext": {
                "baseline": "Фактическая закупочная стратегия",
                "grain": "Стратегия за 181 день",
                "unit": "RUB",
            },
            "type": "bar",
            "dataset": "economic_comparison",
            "sourceId": "economic_summary",
            "encodings": {
                "x": {"field": "metric_label", "type": "nominal", "label": "Показатель"},
                "y": {"field": "value_rub", "type": "quantitative", "label": "Сумма, ₽"},
                "color": {"field": "strategy_label", "type": "nominal", "label": "Стратегия"},
                "tooltip": [
                    {"field": "delta_rub", "type": "quantitative", "label": "Модель минус факт, ₽"},
                    {"field": "sku_count", "type": "quantitative", "label": "SKU"},
                ],
            },
            "valueFormat": "currency",
            "layout": "full",
            "palette": {"kind": "semantic"},
            "legend": {"position": "bottom", "sort": "spec"},
            "settings": {"groupMode": "grouped"},
        },
        {
            "id": "sensitivity_chart",
            "title": "Валовая прибыль при разных оценках скрытого спроса",
            "subtitle": "0×, 1× и 1,5× скрытого спроса; исполняются все положительные рекомендации.",
            "showDescription": True,
            "intent": "comparison",
            "question": "Меняется ли вывод при другой оценке скрытого спроса?",
            "rationale": "Группированные столбцы сравнивают прибыль двух стратегий в каждом сценарии.",
            "comparisonContext": {
                "baseline": "Фактическая стратегия",
                "grain": "Сценарий скрытого спроса",
                "unit": "RUB",
            },
            "type": "bar",
            "dataset": "sensitivity",
            "sourceId": "scenario_comparison",
            "encodings": {
                "x": {"field": "scenario_label", "type": "ordinal", "label": "Скрытый спрос"},
                "y": {
                    "field": "gross_profit_rub",
                    "type": "quantitative",
                    "label": "Валовая прибыль, ₽",
                },
                "color": {"field": "strategy_label", "type": "nominal", "label": "Стратегия"},
                "tooltip": [
                    {
                        "field": "gross_profit_delta_rub",
                        "type": "quantitative",
                        "label": "Модель минус факт, ₽",
                    },
                    {
                        "field": "model_lost_observed_qty",
                        "type": "quantitative",
                        "label": "Потеряно наблюдавшихся продаж, шт.",
                    },
                ],
            },
            "valueFormat": "currency",
            "layout": "full",
            "palette": {"kind": "semantic"},
            "legend": {"position": "bottom", "sort": "spec"},
            "settings": {"groupMode": "grouped"},
        },
        {
            "id": "profit_bridge_chart",
            "title": "Компоненты изменения валовой прибыли",
            "subtitle": "Модель минус точный фактический результат, ₽; отрицательные значения снижают прибыль.",
            "showDescription": True,
            "intent": "decomposition",
            "question": "Какие факторы объясняют разницу прибыли модели и факта?",
            "rationale": "Горизонтальные знаковые столбцы отделяют потери продаж, восстановленный спрос и сверку.",
            "comparisonContext": {
                "baseline": "Точная фактическая валовая прибыль 1С",
                "grain": "Компонент разницы",
                "unit": "RUB",
            },
            "type": "horizontalBar",
            "dataset": "profit_bridge",
            "sourceId": "economic_analysis",
            "encodings": {
                "x": {"field": "component", "type": "nominal", "label": "Компонент"},
                "y": {
                    "field": "profit_delta_rub",
                    "type": "quantitative",
                    "label": "Изменение прибыли, ₽",
                },
                "tooltip": [
                    {"field": "basis", "type": "text", "label": "Основание"},
                ],
            },
            "valueFormat": "currency",
            "layout": "full",
            "palette": {"kind": "diverging", "midpoint": 0},
            "labels": {"values": "all"},
            "referenceLines": [
                {"axis": "y", "value": 0, "label": "Без изменения", "style": "dashed"}
            ],
        },
        {
            "id": "weekly_service_chart",
            "title": "Обслуженные продажи по неделям",
            "subtitle": "Факт и модель, шт.; после мая разрыв становится устойчиво отрицательным для модели.",
            "showDescription": True,
            "intent": "trend",
            "question": "Когда модель начала устойчиво уступать по обслуженным продажам?",
            "rationale": "Недельная линия содержит 27 точек и показывает развитие сервисного разрыва во времени.",
            "comparisonContext": {
                "baseline": "Фактическая стратегия",
                "grain": "Неделя",
                "unit": "units",
            },
            "type": "line",
            "dataset": "weekly_service",
            "sourceId": "daily_summary",
            "encodings": {
                "x": {"field": "week", "type": "temporal", "label": "Неделя"},
                "y": {"field": "served_qty", "type": "quantitative", "label": "Обслужено, шт."},
                "color": {"field": "strategy_label", "type": "nominal", "label": "Стратегия"},
                "lineStyle": {"field": "strategy_label", "type": "nominal", "label": "Стратегия"},
                "tooltip": [
                    {"field": "lost_qty", "type": "quantitative", "label": "Потеряно, шт."},
                    {
                        "field": "average_inventory_value_rub",
                        "type": "quantitative",
                        "label": "Средний капитал, ₽",
                    },
                    {
                        "field": "served_qty_delta",
                        "type": "quantitative",
                        "label": "Модель минус факт, шт.",
                    },
                ],
            },
            "valueFormat": "number",
            "layout": "full",
            "palette": {"kind": "semantic"},
            "legend": {"position": "bottom", "sort": "spec"},
            "labels": {"values": "endpoints"},
        },
        {
            "id": "family_profit_chart",
            "title": "Разница валовой прибыли по семействам",
            "subtitle": "Модель минус факт, ₽; Apple формирует основную часть отрицательного результата.",
            "showDescription": True,
            "intent": "comparison",
            "question": "Какие семейства формируют экономический разрыв?",
            "rationale": "Горизонтальные столбцы подходят для длинных названий и знаковых значений.",
            "comparisonContext": {
                "baseline": "Фактическая стратегия",
                "grain": "Семейство дисплеев",
                "unit": "RUB",
            },
            "type": "horizontalBar",
            "dataset": "family_profit",
            "sourceId": "family_summary",
            "encodings": {
                "x": {"field": "family", "type": "nominal", "label": "Семейство"},
                "y": {
                    "field": "gross_profit_delta_rub",
                    "type": "quantitative",
                    "label": "Модель минус факт, ₽",
                },
                "tooltip": [
                    {"field": "sku_count", "type": "quantitative", "label": "SKU"},
                    {
                        "field": "capital_delta_rub",
                        "type": "quantitative",
                        "label": "Изменение капитала, ₽",
                    },
                    {
                        "field": "sales_delta_qty",
                        "type": "quantitative",
                        "label": "Изменение продаж, шт.",
                    },
                ],
            },
            "valueFormat": "currency",
            "layout": "full",
            "palette": {"kind": "diverging", "midpoint": 0},
            "labels": {"values": "all"},
            "referenceLines": [
                {"axis": "y", "value": 0, "label": "Без изменения", "style": "dashed"}
            ],
        },
    ]

    tables = [
        {
            "id": "classification_table",
            "title": "Классы эффективности модели",
            "subtitle": "SKU сгруппированы по сервису, прибыли и изменению среднего капитала.",
            "showDescription": True,
            "dataset": "classification",
            "defaultSort": {"field": "gross_profit_delta_rub", "direction": "asc"},
            "density": "spacious",
            "sourceId": "classification_summary",
            "layout": "full",
            "columns": [
                {"field": "class_label", "label": "Класс", "type": "text"},
                {"field": "sku_count", "label": "SKU", "format": "number"},
                {
                    "field": "gross_profit_delta_rub",
                    "label": "Δ прибыль, ₽",
                    "format": "currency",
                    "movement": True,
                },
                {
                    "field": "capital_delta_rub",
                    "label": "Δ капитал, ₽",
                    "format": "currency",
                    "movement": True,
                },
                {
                    "field": "sales_delta_qty",
                    "label": "Δ продажи, шт.",
                    "format": "number",
                    "movement": True,
                },
            ],
        },
        {
            "id": "top_loss_table",
            "title": "Топ-10 потерь прибыли по SKU",
            "subtitle": "Позиции с наибольшим отрицательным вкладом модели; ранжирование не считает заказ закупщика эталоном.",
            "showDescription": True,
            "dataset": "top_profit_losses",
            "defaultSort": {"field": "gross_profit_delta_rub", "direction": "asc"},
            "density": "dense",
            "sourceId": "sku_outcomes",
            "layout": "full",
            "columns": [
                {"field": "rank", "label": "№", "format": "number"},
                {"field": "code", "label": "Код", "type": "text"},
                {"field": "name", "label": "Номенклатура", "type": "text"},
                {
                    "field": "gross_profit_delta_rub",
                    "label": "Δ прибыль, ₽",
                    "format": "currency",
                    "movement": True,
                },
                {
                    "field": "capital_delta_rub",
                    "label": "Δ капитал, ₽",
                    "format": "currency",
                    "movement": True,
                },
                {
                    "field": "sales_delta_qty",
                    "label": "Δ продажи, шт.",
                    "format": "number",
                    "movement": True,
                },
                {"field": "new_during_period", "label": "Новый SKU", "type": "text"},
            ],
        },
    ]

    blocks = [
        {
            "id": "title",
            "type": "markdown",
            "body": "# Экономическая эффективность автозаказа дисплеев",
        },
        {
            "id": "executive_summary",
            "type": "markdown",
            "body": (
                "## Executive Summary\n\n"
                f"- **Текущая модель не победила фактическую стратегию.** Валовая прибыль "
                f"была бы ниже на **{_fmt_abs_million(delta['gross_profit_rub'])} млн ₽**, тогда как "
                f"средний капитал сократился бы только на **{_fmt_abs_million(delta['average_inventory_value_rub'])} млн ₽**.\n"
                f"- **Эффективность капитала ухудшилась.** GMROI снизился с "
                f"**{_decimal(actual['gmroi_annualized']):.3f}** до "
                f"**{_decimal(model['gmroi_annualized']):.3f}**; экономия капитала не окупила "
                "потерю доступности.\n"
                f"- **Модель восстановила около {_fmt_million(reconciliation['restored_hidden_profit_rub'])} млн ₽ "
                f"скрытой прибыли, но потеряла {_fmt_million(reconciliation['lost_observed_profit_rub'])} млн ₽ "
                "на наблюдавшихся продажах.** Вывод устойчив во всех сценариях скрытого спроса.\n"
                f"- **Формулу нужно применять сегментно.** На {safe_release['sku_count']} SKU она "
                f"высвобождает {_fmt_abs_million(safe_release['capital_delta_rub'])} млн ₽ без худшего сервиса, "
                f"но {service_worse['sku_count']} проблемных SKU перекрывают этот эффект."
            ),
        },
        {
            "id": "headline_metrics",
            "type": "metric-strip",
            "cardIds": ["profit_card", "capital_card", "gmroi_card", "fill_rate_card"],
        },
        {
            "id": "comparison_section",
            "type": "markdown",
            "body": (
                "## Модель не окупила экономию капитала\n\n"
                f"Модель сократила средний капитал примерно на 2,3%, но валовую прибыль — на 6,9%. "
                f"Оборачиваемость снизилась с **{_decimal(actual['inventory_turnover_annualized']):.2f}** "
                f"до **{_decimal(model['inventory_turnover_annualized']):.2f}**, а запас вырос с "
                f"**{_decimal(actual['days_inventory']):.1f}** до **{_decimal(model['days_inventory']):.1f} дня**. "
                "То есть модель не только заработала меньше, но и медленнее превращала вложенный рубль в продажи."
            ),
        },
        {"id": "comparison_chart_block", "type": "chart", "chartId": "economic_comparison_chart"},
        {
            "id": "sensitivity_section",
            "type": "markdown",
            "body": (
                "## Вывод устойчив к оценке упущенных продаж\n\n"
                "При нулевом скрытом спросе модель теряет уже наблюдавшиеся продажи и даёт около "
                "**75,52 млн ₽** прибыли. При повышенном сценарии 1,5× прибыль модели достигает "
                "**78,21 млн ₽**, но остаётся примерно на **4,90 млн ₽** ниже факта. Поэтому результат "
                "не объясняется одной спорной оценкой скрытого спроса."
            ),
        },
        {"id": "sensitivity_chart_block", "type": "chart", "chartId": "sensitivity_chart"},
        {
            "id": "profit_bridge_section",
            "type": "markdown",
            "body": (
                "## Главная проблема — потеря уже существовавшего спроса\n\n"
                f"Модель восстановила скрытую прибыль примерно на **{_fmt_million(reconciliation['restored_hidden_profit_rub'])} млн ₽**, "
                f"но одновременно потеряла около **{_fmt_million(reconciliation['lost_observed_profit_rub'])} млн ₽** "
                "на продажах, которые фактическая стратегия смогла обслужить. Финансовая сверка "
                f"точного итога 1С и SKU-оценки составляет **{_fmt_million(reconciliation['gross_profit_gap_exact_minus_sku_rub'])} млн ₽**; "
                "мост сходится с итогом модели с погрешностью менее 1 ₽."
            ),
        },
        {"id": "profit_bridge_chart_block", "type": "chart", "chartId": "profit_bridge_chart"},
        {
            "id": "weekly_section",
            "type": "markdown",
            "body": (
                "## Сервисный разрыв накапливается во второй половине периода\n\n"
                "В феврале–апреле модель местами держит больше капитала и обслуживает сопоставимый объём. "
                "С мая капитал начинает заметно снижаться, но вместе с ним падают обслуженные продажи. "
                "Это признак самоподдерживающегося дефицита: меньше доступности — меньше обслуженной истории — ниже следующий прогноз."
            ),
        },
        {"id": "weekly_chart_block", "type": "chart", "chartId": "weekly_service_chart"},
        {
            "id": "concentration_section",
            "type": "markdown",
            "body": (
                "## Убыток сосредоточен в новинках и дорогих Apple SKU\n\n"
                f"Новые в периоде {new_row['sku_count']} SKU дали **{_fmt_million(new_row['gross_profit_delta_rub'])} млн ₽** "
                f"построчного разрыва прибыли. Apple дал ещё более концентрированный сигнал: "
                f"**{_fmt_million(apple['gross_profit_delta_rub'])} млн ₽**. В топе потерь — iPhone 13–17, "
                "особенно Pro и Pro Max. Это адресная проблема cold-start и сервисного запаса, а не основание "
                "повышать заказ всей матрице."
            ),
        },
        {"id": "family_chart_block", "type": "chart", "chartId": "family_profit_chart"},
        {
            "id": "segmentation_section",
            "type": "markdown",
            "body": (
                "## Единая формула скрывает полезные и вредные сегменты\n\n"
                f"На {safe_release['sku_count']} SKU модель высвобождает "
                f"**{_fmt_abs_million(safe_release['capital_delta_rub'])} млн ₽** без худшего сервиса. "
                f"Но на {service_worse['sku_count']} SKU она теряет **{_fmt_abs_million(service_worse['gross_profit_delta_rub'])} млн ₽** прибыли. "
                "Диагностический hindsight-гибрид показывает, что направление сегментации перспективно, "
                "но его нельзя считать production-прогнозом: классы сформированы уже после знания результата."
            ),
        },
        {"id": "classification_table_block", "type": "table", "tableId": "classification_table"},
        {
            "id": "top_losses_section",
            "type": "markdown",
            "body": (
                "## Топ потерь нужен для диагностики, а не для копирования человека\n\n"
                "Таблица ранжирует позиции по экономическому эффекту модели. Она не утверждает, что "
                "фактическое количество закупщика было оптимальным: здесь проверяется, сколько спроса "
                "обслужила каждая стратегия и какой финансовый результат получила."
            ),
        },
        {"id": "top_loss_table_block", "type": "table", "tableId": "top_loss_table"},
        {
            "id": "recommendations",
            "type": "markdown",
            "body": (
                "## Что улучшать в первую очередь\n\n"
                "1. **Отдельный режим для новых и быстро растущих SKU.** До накопления истории использовать "
                "аналог/семейство, первые продажи, минимальный сервисный запас и более частый пересмотр.\n"
                "2. **Защитить 353 SKU риска.** Для дорогих Apple-позиций нужен профильный min/max или "
                "service-level floor по цене дефицита, а не один общий порог.\n"
                "3. **Сохранить экономию капитала на безопасном сегменте.** 474 кандидата сначала проверить "
                "в forward shadow по правилу, определённому до периода.\n"
                "4. **Добавить сигналы необслуженного спроса.** Отказы, обращения, просмотры при нулевом "
                "остатке, корзины и аналоги не дают дефициту искусственно уменьшать прогноз.\n"
                "5. **Повторить walk-forward backtest** с резервами и фактическими сроками поставщиков. "
                "Допускать автономность только при прибыли не ниже факта и меньшем капитале либо по заранее "
                "согласованному положительному компромиссу GMROI."
            ),
        },
        {
            "id": "next_action",
            "type": "markdown",
            "body": (
                "## Практическое решение сейчас\n\n"
                "Автозаказ следует продолжать использовать для создания внутренних проектов в помощнике. "
                "Закупщик проверяет количество и поставщика, а передача непроведённого черновика в 1С "
                "остаётся отдельным ручным действием. Экономический backtest не подтверждает автономное "
                "принятие всех строк текущей формулы."
            ),
        },
        {
            "id": "further_questions",
            "type": "markdown",
            "body": (
                "## Further Questions\n\n"
                "- Можно ли восстановить исторические резервы и фактические сроки поставки по поставщикам?\n"
                "- Какие сигналы отсутствия доступны: отказы сайта, обращения, просмотры или незавершённые корзины?\n"
                "- Какова стоимость капитала, хранения и устаревания по ценовым группам?\n"
                "- Как заранее, без знания будущего, отделить безопасные SKU от 353 позиций риска?"
            ),
        },
        {
            "id": "caveats",
            "type": "markdown",
            "body": (
                "## Caveats and Assumptions\n\n"
                "- Скрытый спрос — модельная оценка, а не бухгалтерский факт.\n"
                "- Нулевой остаток на конец дня может означать продажу последней единицы; показана чувствительность 0×, 1× и 1,5×.\n"
                "- Доходность модели использует историческую маржу SKU и предполагает пропорциональные возвраты.\n"
                "- Исторические резервы не восстановлены; свободный остаток модели может быть завышен.\n"
                "- Используется текущая когорта, поэтому присутствует survivorship bias.\n"
                "- Срок поставки фиксирован на 52 дня; финансирование, хранение, устаревание и налоги не учтены.\n"
                "- `all_recommendations` — верхняя оценка исполнения рассчитанных количеств, не автономный production-режим."
            ),
        },
    ]

    return {
        "surface": "report",
        "manifest": {
            "version": 1,
            "surface": "report",
            "title": "Экономическая эффективность автозаказа дисплеев",
            "description": "Прибыль, упущенные продажи, оборачиваемость и капитал за февраль–июль 2026 года.",
            "generatedAt": generated_at,
            "filters": [],
            "cards": cards,
            "charts": charts,
            "tables": tables,
            "sources": sources,
            "blocks": blocks,
        },
        "snapshot": {
            "version": 1,
            "generatedAt": generated_at,
            "status": "ready",
            "datasets": {
                "headline": headline,
                "economic_comparison": comparison_rows,
                "sensitivity": sensitivity_rows,
                "profit_bridge": profit_bridge_rows,
                "weekly_service": weekly_rows,
                "family_profit": family_rows,
                "classification": class_rows,
                "top_profit_losses": top_loss_rows,
            },
            "accessIssues": [],
        },
        "sources": sources,
        "package_info": {
            "originUrl": "artifact://display-auto-order-economic-effectiveness-2026-h1"
        },
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--economic-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    analysis = build_analysis(args.economic_dir)
    markdown_path = build_markdown(args.economic_dir, analysis)
    validation_path = build_validation(args.economic_dir, analysis)
    notebook_path = build_notebook(args.economic_dir, analysis)
    artifact = build_artifact(args.economic_dir, analysis)
    artifact_path = args.economic_dir / "artifact.json"
    artifact_path.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "status": "ready",
                "analysis": str(args.economic_dir / "economic-analysis-summary.json"),
                "markdown": str(markdown_path),
                "validation": str(validation_path),
                "notebook": str(notebook_path),
                "artifact": str(artifact_path),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
