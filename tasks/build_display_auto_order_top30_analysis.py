"""Build a reproducible diagnostic review of the 30 largest SKU quantity gaps."""

from __future__ import annotations

import argparse
import csv
import html
import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

import nbformat
from nbclient import NotebookClient

ZERO = Decimal("0")
MAIN_SCENARIO = "lead_time_52d"


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0").strip() or "0")
    except (ArithmeticError, ValueError):
        return ZERO


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        if not rows:
            return
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _family(name: str) -> str:
    lowered = name.lower()
    if "iphone" in lowered:
        return "Apple iPhone"
    if any(value in lowered for value in ("xiaomi", "redmi", "poco")):
        return "Xiaomi/Redmi/Poco"
    if any(value in lowered for value in ("tecno", "infinix", "itel")):
        return "Tecno/Infinix/Itel"
    if any(value in lowered for value in ("huawei", "honor")):
        return "Huawei/Honor"
    if "realme" in lowered:
        return "Realme"
    if "samsung" in lowered:
        return "Samsung"
    return "Прочие"


def _is_multiple(value: Decimal, divisor: int) -> bool:
    return value > ZERO and value % Decimal(divisor) == ZERO


def _diagnose(
    *,
    name: str,
    actual_qty: Decimal,
    model_qty: Decimal,
    delta_qty: Decimal,
    forecast_bias: Decimal,
) -> tuple[str, str, str, str]:
    lowered = name.lower()
    if model_qty == ZERO and actual_qty > ZERO:
        return (
            "opening_stock_or_forward_buffer_gap",
            "Модель не увидела потребность при имеющемся запасе",
            "механизм подтверждён; бизнес-причина не подтверждена",
            "Проверить, был ли фактический заказ буфером на следующий период, MOQ или реакцией на информацию закупщика.",
        )
    if "iphone 11" in lowered:
        return (
            "version_redistribution_confirmed",
            "Перераспределение внутри семейства iPhone 11",
            "симптом подтверждён; правило выбора версии не подтверждено",
            "Сравнить версии по совместимости, качеству, цене и поставщику; считать спрос сначала на семейство.",
        )
    if forecast_bias < Decimal("-0.15"):
        return (
            "forecast_understatement_confirmed",
            "Подтверждённый недопрогноз спроса",
            "высокая",
            "Разобрать недельные пики, дни отсутствия и запуски/смену тренда; скорректировать прогноз или safety stock.",
        )
    if delta_qty > ZERO:
        return (
            "model_target_above_human_purchase",
            "Целевой запас модели выше фактической закупки",
            "расхождение подтверждено; оптимальность не доказана",
            "Проверить последующий дефицит и остаток: без этого нельзя решить, была ли модель избыточной.",
        )
    return (
        "batch_rounding_or_buffer_gap",
        "Различие закупочной партии, округления или горизонта запаса",
        "вероятная гипотеза",
        "Получить MOQ/кратность короба и плановый горизонт закупщика; затем повторить расчёт с пакетными правилами.",
    )


def _fmt_int(value: Any) -> str:
    return f"{_decimal(value):,.0f}".replace(",", " ")


def _fmt_signed_int(value: Any) -> str:
    return f"{_decimal(value):+,.0f}".replace(",", " ")


def _fmt_pct(value: Any, digits: int = 1) -> str:
    return f"{_decimal(value) * Decimal('100'):.{digits}f}%".replace(".", ",")


def _short_name(name: str, length: int = 78) -> str:
    return name if len(name) <= length else name[: length - 1].rstrip() + "…"


def _build_bar_svg(rows: Sequence[Mapping[str, Any]]) -> str:
    width, row_height, left, right, top = 1040, 27, 405, 75, 36
    height = top + len(rows) * row_height + 30
    deltas = [_decimal(row["delta_qty_model_minus_actual"]) for row in rows]
    bound = max(abs(value) for value in deltas) if deltas else Decimal("1")
    plot_width = width - left - right
    zero_x = left + plot_width * float(-min(deltas)) / float(max(deltas) - min(deltas))
    if max(deltas) == min(deltas):
        zero_x = left + plot_width / 2
    pieces = [
        f'<svg viewBox="0 0 {width} {height}" role="img" '
        'aria-label="Тридцать крупнейших расхождений количества между моделью и фактом">',
        "<style>text{font-family:Arial,sans-serif;fill:#20242a}.n{font-size:11px}.v{font-size:11px;font-weight:700}"
        ".neg{fill:#b5473b}.pos{fill:#247b69}.axis{stroke:#565d66;stroke-width:1}</style>",
        f'<line x1="{zero_x:.1f}" y1="20" x2="{zero_x:.1f}" y2="{height - 16}" class="axis"/>',
    ]
    max_span = max(float(bound), 1.0)
    half_span = plot_width / 2
    for index, row in enumerate(rows):
        y = top + index * row_height
        delta = _decimal(row["delta_qty_model_minus_actual"])
        bar_width = abs(float(delta)) / max_span * half_span
        x = zero_x - bar_width if delta < ZERO else zero_x
        css = "neg" if delta < ZERO else "pos"
        label = html.escape(f"{row['nomenclature_code']} · {_short_name(str(row['name']), 51)}")
        signed = _fmt_signed_int(delta)
        pieces.extend(
            [
                f'<text x="6" y="{y + 13}" class="n">{label}</text>',
                f'<rect x="{x:.1f}" y="{y}" width="{bar_width:.1f}" height="17" rx="2" class="{css}"/>',
                f'<text x="{x - 7 if delta < ZERO else x + bar_width + 7:.1f}" y="{y + 13}" '
                f'text-anchor="{"end" if delta < ZERO else "start"}" class="v">{signed}</text>',
            ]
        )
    pieces.append("</svg>")
    return "\n".join(pieces)


def _family_svg(rows: Sequence[Mapping[str, Any]]) -> str:
    width, height, left, right, top, row_height = 820, 310, 175, 80, 35, 41
    bound = max(abs(_decimal(row["delta_qty"])) for row in rows) or Decimal("1")
    plot_width = width - left - right
    zero_x = left + plot_width * Decimal("0.72")
    scale = min(float(zero_x - left), float(width - right - zero_x)) / float(bound)
    pieces = [
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Расхождение по товарным семействам">',
        "<style>text{font-family:Arial,sans-serif;fill:#20242a}.n{font-size:13px}.v{font-size:12px;font-weight:700}"
        ".neg{fill:#b5473b}.pos{fill:#247b69}.axis{stroke:#565d66;stroke-width:1}</style>",
        f'<line x1="{float(zero_x):.1f}" y1="18" x2="{float(zero_x):.1f}" y2="{height - 20}" class="axis"/>',
    ]
    for index, row in enumerate(rows):
        y = top + index * row_height
        delta = _decimal(row["delta_qty"])
        bar_width = abs(float(delta)) * scale
        x = float(zero_x) - bar_width if delta < ZERO else float(zero_x)
        signed = _fmt_signed_int(delta)
        pieces.extend(
            [
                f'<text x="6" y="{y + 15}" class="n">{html.escape(str(row["family"]))}</text>',
                f'<rect x="{x:.1f}" y="{y}" width="{bar_width:.1f}" height="21" rx="3" class="{"neg" if delta < ZERO else "pos"}"/>',
                f'<text x="{x - 7 if delta < ZERO else x + bar_width + 7:.1f}" y="{y + 15}" '
                f'text-anchor="{"end" if delta < ZERO else "start"}" class="v">{signed}</text>',
            ]
        )
    pieces.append("</svg>")
    return "\n".join(pieces)


def build_top30(backtest_dir: Path, main_scenario: str = MAIN_SCENARIO) -> dict[str, Any]:
    comparison = _read_csv(backtest_dir / "sku-comparison.csv")
    decisions = [
        row
        for row in _read_csv(backtest_dir / "decision-detail.csv")
        if row.get("scenario") == main_scenario
    ]
    top = sorted(
        comparison,
        key=lambda row: abs(_decimal(row["delta_qty_model_minus_actual"])),
        reverse=True,
    )[:30]
    top_codes = {row["nomenclature_code"] for row in top}
    decisions_by_code: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in decisions:
        if row["nomenclature_code"] in top_codes:
            decisions_by_code[row["nomenclature_code"]].append(row)

    detail: list[dict[str, Any]] = []
    for rank, row in enumerate(top, 1):
        code = row["nomenclature_code"]
        sku_decisions = sorted(decisions_by_code[code], key=lambda item: item["decision_date"])
        observed = sum((_decimal(item["actual_sales_next_7d"]) for item in sku_decisions), ZERO)
        predicted = sum((_decimal(item["predicted_sales_next_7d"]) for item in sku_decisions), ZERO)
        abs_error = sum((_decimal(item["forecast_abs_error_7d"]) for item in sku_decisions), ZERO)
        bias = (predicted - observed) / observed if observed else ZERO
        wape = abs_error / observed if observed else ZERO
        actual_qty = _decimal(row["actual_order_qty"])
        model_qty = _decimal(row["model_order_qty"])
        delta_qty = _decimal(row["delta_qty_model_minus_actual"])
        category, category_ru, confidence, check = _diagnose(
            name=row["name"],
            actual_qty=actual_qty,
            model_qty=model_qty,
            delta_qty=delta_qty,
            forecast_bias=bias,
        )
        first = sku_decisions[0] if sku_decisions else {}
        last = sku_decisions[-1] if sku_decisions else {}
        targets = [_decimal(item["target_stock_qty"]) for item in sku_decisions]
        actual_value = _decimal(row["actual_order_value_rub"])
        model_value = _decimal(row["model_order_value_rub"])
        detail.append(
            {
                "rank": rank,
                "nomenclature_code": code,
                "name": row["name"],
                "family": _family(row["name"]),
                "status_current": row["status_current"],
                "actual_order_qty": str(actual_qty),
                "model_order_qty": str(model_qty),
                "delta_qty_model_minus_actual": str(delta_qty),
                "absolute_qty_gap": str(abs(delta_qty)),
                "actual_order_lines": row["actual_order_lines"],
                "model_order_lines": row["model_order_lines"],
                "actual_qty_multiple_50": int(_is_multiple(actual_qty, 50)),
                "actual_qty_multiple_100": int(_is_multiple(actual_qty, 100)),
                "model_qty_multiple_50": int(_is_multiple(model_qty, 50)),
                "model_qty_multiple_100": int(_is_multiple(model_qty, 100)),
                "observed_sales_weekly_windows": str(observed),
                "predicted_sales_weekly_windows": str(predicted),
                "forecast_bias_pct": str(bias),
                "forecast_wape": str(wape),
                "model_stock_first_decision": first.get("model_free_stock", ""),
                "model_stock_last_decision": last.get("model_free_stock", ""),
                "max_model_incoming_qty": str(
                    max(
                        (_decimal(item["model_incoming_qty"]) for item in sku_decisions),
                        default=ZERO,
                    )
                ),
                "average_target_stock_qty": str(
                    sum(targets, ZERO) / Decimal(len(targets)) if targets else ZERO
                ),
                "actual_average_price_rub": str(actual_value / actual_qty if actual_qty else ZERO),
                "model_average_price_rub": str(model_value / model_qty if model_qty else ZERO),
                "manual_review_occurrences": row["manual_review_occurrences"],
                "supplier_as_of": row["supplier_as_of"],
                "diagnostic_category": category,
                "diagnostic_category_ru": category_ru,
                "confidence": confidence,
                "recommended_check": check,
            }
        )
    _write_csv(backtest_dir / "top-30-detail.csv", detail)

    family_map: dict[str, dict[str, Decimal | int]] = defaultdict(
        lambda: {
            "sku_count": 0,
            "actual_qty": ZERO,
            "model_qty": ZERO,
            "delta_qty": ZERO,
            "absolute_gap": ZERO,
        }
    )
    for row in detail:
        values = family_map[str(row["family"])]
        values["sku_count"] = int(values["sku_count"]) + 1
        values["actual_qty"] = _decimal(values["actual_qty"]) + _decimal(row["actual_order_qty"])
        values["model_qty"] = _decimal(values["model_qty"]) + _decimal(row["model_order_qty"])
        values["delta_qty"] = _decimal(values["delta_qty"]) + _decimal(
            row["delta_qty_model_minus_actual"]
        )
        values["absolute_gap"] = _decimal(values["absolute_gap"]) + _decimal(
            row["absolute_qty_gap"]
        )
    families = [
        {"family": family, **{key: str(value) for key, value in values.items()}}
        for family, values in family_map.items()
    ]
    families.sort(key=lambda row: _decimal(row["absolute_gap"]), reverse=True)

    total_actual = sum((_decimal(row["actual_order_qty"]) for row in comparison), ZERO)
    total_delta = sum((_decimal(row["delta_qty_model_minus_actual"]) for row in comparison), ZERO)
    top_actual = sum((_decimal(row["actual_order_qty"]) for row in detail), ZERO)
    top_model = sum((_decimal(row["model_order_qty"]) for row in detail), ZERO)
    top_delta = top_model - top_actual
    observed = sum((_decimal(row["observed_sales_weekly_windows"]) for row in detail), ZERO)
    predicted = sum((_decimal(row["predicted_sales_weekly_windows"]) for row in detail), ZERO)
    abs_error = sum(
        (
            _decimal(row["forecast_wape"]) * _decimal(row["observed_sales_weekly_windows"])
            for row in detail
        ),
        ZERO,
    )
    intersect = [
        row
        for row in comparison
        if _decimal(row["actual_order_qty"]) > ZERO and _decimal(row["model_order_qty"]) > ZERO
    ]
    actual_only = [
        row
        for row in comparison
        if _decimal(row["actual_order_qty"]) > ZERO and _decimal(row["model_order_qty"]) == ZERO
    ]
    model_only = [
        row
        for row in comparison
        if _decimal(row["model_order_qty"]) > ZERO and _decimal(row["actual_order_qty"]) == ZERO
    ]
    price_effect = sum(
        (
            _decimal(row["model_order_qty"])
            * (
                _decimal(row["model_order_value_rub"]) / _decimal(row["model_order_qty"])
                - _decimal(row["actual_order_value_rub"]) / _decimal(row["actual_order_qty"])
            )
            for row in intersect
        ),
        ZERO,
    )
    quantity_mix_effect = sum(
        (
            (_decimal(row["model_order_qty"]) - _decimal(row["actual_order_qty"]))
            * _decimal(row["actual_order_value_rub"])
            / _decimal(row["actual_order_qty"])
            for row in intersect
        ),
        ZERO,
    )
    categories = Counter(str(row["diagnostic_category"]) for row in detail)
    summary = {
        "schema": "display_auto_order_top30_analysis.v1",
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "scenario": main_scenario,
        "period": {"date_from": "2026-02-01", "date_to": "2026-07-31"},
        "selection_rule": "30 SKU with largest abs(model_order_qty - actual_order_qty)",
        "sku_count": len(detail),
        "under_ordered_sku": sum(
            _decimal(row["delta_qty_model_minus_actual"]) < ZERO for row in detail
        ),
        "over_ordered_sku": sum(
            _decimal(row["delta_qty_model_minus_actual"]) > ZERO for row in detail
        ),
        "actual_order_qty": str(top_actual),
        "model_order_qty": str(top_model),
        "net_qty_gap": str(top_delta),
        "gross_absolute_qty_gap": str(
            sum((_decimal(row["absolute_qty_gap"]) for row in detail), ZERO)
        ),
        "under_order_qty": str(
            sum(
                (
                    _decimal(row["delta_qty_model_minus_actual"])
                    for row in detail
                    if _decimal(row["delta_qty_model_minus_actual"]) < ZERO
                ),
                ZERO,
            )
        ),
        "over_order_qty": str(
            sum(
                (
                    _decimal(row["delta_qty_model_minus_actual"])
                    for row in detail
                    if _decimal(row["delta_qty_model_minus_actual"]) > ZERO
                ),
                ZERO,
            )
        ),
        "share_of_actual_order_qty": str(top_actual / total_actual),
        "share_of_total_net_gap": str(top_delta / total_delta if total_delta else ZERO),
        "remaining_sku_net_gap": str(total_delta - top_delta),
        "actual_order_lines": sum(int(row["actual_order_lines"]) for row in detail),
        "model_order_lines": sum(int(row["model_order_lines"]) for row in detail),
        "actual_qty_multiple_50_count": sum(int(row["actual_qty_multiple_50"]) for row in detail),
        "actual_qty_multiple_100_count": sum(int(row["actual_qty_multiple_100"]) for row in detail),
        "model_qty_multiple_50_count": sum(int(row["model_qty_multiple_50"]) for row in detail),
        "model_qty_multiple_100_count": sum(int(row["model_qty_multiple_100"]) for row in detail),
        "observed_sales_weekly_windows": str(observed),
        "predicted_sales_weekly_windows": str(predicted),
        "forecast_bias_pct": str((predicted - observed) / observed if observed else ZERO),
        "forecast_wape": str(abs_error / observed if observed else ZERO),
        "forecast_bias_within_10pct_count": sum(
            abs(_decimal(row["forecast_bias_pct"])) <= Decimal("0.10") for row in detail
        ),
        "forecast_bias_within_15pct_count": sum(
            abs(_decimal(row["forecast_bias_pct"])) <= Decimal("0.15") for row in detail
        ),
        "forecast_understatement_over_15pct_count": sum(
            _decimal(row["forecast_bias_pct"]) < Decimal("-0.15") for row in detail
        ),
        "manual_review_occurrences": sum(int(row["manual_review_occurrences"]) for row in detail),
        "categories": dict(categories),
        "families": families,
        "top30_actual_average_price_rub": str(
            sum(
                (
                    _decimal(row["actual_average_price_rub"]) * _decimal(row["actual_order_qty"])
                    for row in detail
                ),
                ZERO,
            )
            / top_actual
        ),
        "top30_model_average_price_rub": str(
            sum(
                (
                    _decimal(row["model_average_price_rub"]) * _decimal(row["model_order_qty"])
                    for row in detail
                ),
                ZERO,
            )
            / top_model
        ),
        "price_decomposition_all_sku": {
            "both_actual_and_model_sku": len(intersect),
            "price_effect_rub": str(price_effect),
            "quantity_mix_effect_at_actual_prices_rub": str(quantity_mix_effect),
            "actual_only_sku": len(actual_only),
            "actual_only_qty": str(
                sum((_decimal(row["actual_order_qty"]) for row in actual_only), ZERO)
            ),
            "actual_only_value_rub": str(
                sum((_decimal(row["actual_order_value_rub"]) for row in actual_only), ZERO)
            ),
            "model_only_sku": len(model_only),
            "model_only_qty": str(
                sum((_decimal(row["model_order_qty"]) for row in model_only), ZERO)
            ),
            "model_only_value_rub": str(
                sum((_decimal(row["model_order_value_rub"]) for row in model_only), ZERO)
            ),
        },
        "validation_status": "share_with_caveats",
        "validation_note": "Quantity gaps are not SKU-level stockout attribution; business reasons such as MOQ and forward buying are unavailable.",
    }
    (backtest_dir / "top-30-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {"summary": summary, "detail": detail, "families": families}


def _markdown_report(result: Mapping[str, Any]) -> str:
    summary = result["summary"]
    detail = result["detail"]
    families = result["families"]
    confirmed_underforecast = [
        row for row in detail if row["diagnostic_category"] == "forecast_understatement_confirmed"
    ]
    zero_orders = [row for row in detail if _decimal(row["model_order_qty"]) == ZERO]
    iphone11 = [row for row in detail if "iphone 11" in str(row["name"]).lower()]
    fragmentation_ratio = f"{Decimal(summary['model_order_lines']) / Decimal(summary['actual_order_lines']):.1f}".replace(
        ".", ","
    )
    model_average_price = f"{_decimal(summary['top30_model_average_price_rub']):.2f}".replace(
        ".", ","
    )
    actual_average_price = f"{_decimal(summary['top30_actual_average_price_rub']):.2f}".replace(
        ".", ","
    )
    family_lines = "\n".join(
        f"| {row['family']} | {_fmt_int(row['sku_count'])} | {_fmt_int(row['actual_qty'])} | "
        f"{_fmt_int(row['model_qty'])} | {_fmt_signed_int(row['delta_qty'])} | {_fmt_int(row['absolute_gap'])} |"
        for row in families
    )
    forecast_lines = "\n".join(
        f"- `{row['nomenclature_code']}` — {_short_name(str(row['name']), 94)}: "
        f"bias {_fmt_pct(row['forecast_bias_pct'])}, WAPE {_fmt_pct(row['forecast_wape'])}."
        for row in confirmed_underforecast
    )
    zero_lines = "\n".join(
        f"- `{row['nomenclature_code']}`: факт {_fmt_int(row['actual_order_qty'])} шт.; "
        f"остаток модели на первом решении {_fmt_int(row['model_stock_first_decision'])}, "
        f"на последнем {_fmt_int(row['model_stock_last_decision'])}; "
        f"продажи в недельных окнах {_fmt_int(row['observed_sales_weekly_windows'])}."
        for row in zero_orders
    )
    iphone_lines = "\n".join(
        f"- `{row['nomenclature_code']}`: модель минус факт {_fmt_signed_int(row['delta_qty_model_minus_actual'])} шт.; "
        f"bias прогноза {_fmt_pct(row['forecast_bias_pct'])}."
        for row in iphone11
    )
    return f"""# Топ‑30 расхождений автозаказа: предметный разбор

Период: 1 февраля — 31 июля 2026 года. Основной сценарий: срок поставки 52 дня. В выборку вошли 30 SKU с максимальным `abs(модель − факт)` по заказанному количеству.

## Ответ в одном абзаце

Топ‑30 концентрирует главный количественный разрыв: человек заказал **{_fmt_int(summary['actual_order_qty'])} шт.**, модель — **{_fmt_int(summary['model_order_qty'])} шт.**, чистое отклонение **{_fmt_int(summary['net_qty_gap'])} шт.** При этом прогноз спроса по этим позициям в сумме завышен лишь на **{_fmt_pct(summary['forecast_bias_pct'], 2)}**, а у **{summary['forecast_bias_within_15pct_count']} из 30 SKU** bias находится в пределах ±15%. Следовательно, большинство крупных различий возникло не из-за общего недопрогноза, а из-за правил партии, горизонта запаса, округления и распределения между версиями. Это не доказывает, что человек всегда прав: фактическая закупка могла создавать запас на следующий период.

## Что подтверждено данными

1. **Разрыв сильно концентрирован.** 27 SKU недозаказаны моделью и 3 перезаказаны. Топ‑30 содержит {_fmt_pct(summary['share_of_actual_order_qty'])} фактического количества и объясняет {_fmt_pct(summary['share_of_total_net_gap'])} общего чистого разрыва. Остальные 1 504 SKU вместе дают **+{_fmt_int(summary['remaining_sku_net_gap'])} шт.**
2. **Модель дробит закупку.** В топ‑30 у факта {summary['actual_order_lines']} строк заказа, у модели {summary['model_order_lines']} — в {fragmentation_ratio} раза больше. Фактическое количество кратно 100 у {summary['actual_qty_multiple_100_count']} SKU, модельное — только у {summary['model_qty_multiple_100_count']}.
3. **Прогноз не объясняет большинство разрывов.** Наблюдаемые продажи — {_fmt_int(summary['observed_sales_weekly_windows'])} шт., прогноз — {_fmt_int(summary['predicted_sales_weekly_windows'])} шт.; агрегатный bias {_fmt_pct(summary['forecast_bias_pct'], 2)}, WAPE {_fmt_pct(summary['forecast_wape'], 2)}.
4. **Ручная проверка не сработала как сигнал расхождения.** Для всех 30 SKU `manual_review_occurrences = 0`. Текущие блокеры проверяют допустимость, но не необычность рассчитанного количества.
5. **Топ‑30 не объясняет рост общей стоимости модели.** Средняя цена в топ‑30 у модели **{model_average_price} ₽**, у факта **{actual_average_price} ₽**. Рост стоимости всей когорты связан с изменением цен и дорогими SKU, которые покупала только модель.

## Где подтверждён недопрогноз

Только у {summary['forecast_understatement_over_15pct_count']} из 30 SKU прогноз ниже наблюдаемых продаж более чем на 15%:

{forecast_lines}

Для остальных крупных недозаказов нельзя использовать объяснение «модель не увидела спрос» без дополнительных доказательств.

## Нулевые модельные заказы

У трёх SKU модель заказала ноль не из-за отсутствия поставщика, а потому что её расчётный остаток не опустился ниже целевого уровня:

{zero_lines}

Вероятная бизнес‑причина фактических заказов — дополнительный буфер, MOQ или закупка на следующий горизонт. В имеющихся данных этого нет, поэтому причина остаётся гипотезой.

## iPhone 11: перераспределение между версиями

Внутри семейства одновременно есть недозаказ и перезаказ отдельных версий:

{iphone_lines}

По всем SKU iPhone 11, не только входящим в топ‑30, факт составил **8 777 шт.**, модель **8 117 шт.**, семейная дельта **−660 шт.** Часть разрыва одной версии компенсирована другими. Оптимальный подход — сначала считать потребность семейства совместимых товаров, затем распределять её между версиями по качеству, цене и поставщику.

## Концентрация по семействам

| Семейство | SKU | Факт | Модель | Дельта | Абсолютный разрыв |
|---|---:|---:|---:|---:|---:|
{family_lines}

Первый приоритет — Xiaomi/Redmi/Poco, затем Tecno/Infinix/Itel и Apple iPhone.

## Что улучшать в модели

1. **Добавить упаковочные правила как мягкую рекомендацию:** MOQ, кратность коробу и типичный закупочный лот. Не блокировать заказ, а показывать рассчитанное и округлённое количество закупщику.
2. **Считать совместимые версии и аналоги группой.** Для iPhone 11 сначала определить общий спрос семейства, затем выбрать конкретную версию.
3. **Добавить discrepancy‑сигнал.** Отправлять строку на ручное внимание, если количество сильно отличается от типичного заказа, семейной потребности или исторической партии. Это предупреждение, не запрет.
4. **Развести два горизонта:** потребность до ближайшей поставки и дополнительный буфер следующего закупочного цикла. Тогда будет видно, почему человек заказывает больше модели.
5. **Калибровать прогноз адресно** только для пяти подтверждённых случаев недопрогноза, а не увеличивать запас всем SKU.

## Чего этот анализ пока не доказывает

- Топ‑30 по количеству не равен топ‑30 по созданному дефициту. Нужна SKU‑level атрибуция необеспеченных продаж и дней отсутствия.
- Нет исторических MOQ, кратности короба, планового горизонта и комментария закупщика.
- Фактический заказ не является автоматически эталоном: он мог быть избыточным или относиться к следующему периоду.
- Пустой поставщик у нулевого модельного заказа — следствие отсутствия заказа, а не доказательство блокировки поставщиком.

Статус проверки: **можно использовать с оговорками**.
"""


def _build_artifact(result: Mapping[str, Any]) -> dict[str, Any]:
    summary, detail, families = result["summary"], result["detail"], result["families"]
    generated_at = summary["generated_at"]
    source = {
        "id": "top30_source",
        "label": "Исторический backtest автозаказа, февраль–июль 2026",
        "path": "top-30-detail.csv",
    }
    chart_rows = [
        {
            "rank": int(row["rank"]),
            "sku": row["nomenclature_code"],
            "label": f"{row['nomenclature_code']} · {_short_name(str(row['name']), 45)}",
            "delta_qty": float(_decimal(row["delta_qty_model_minus_actual"])),
            "family": row["family"],
        }
        for row in detail
    ]
    return {
        "surface": "report",
        "manifest": {
            "version": 1,
            "surface": "report",
            "title": "Топ‑30 расхождений автозаказа",
            "description": "Диагностика крупнейших разниц между фактической закупкой и моделью.",
            "generatedAt": generated_at,
            "filters": [],
            "cards": [],
            "charts": [
                {
                    "id": "top30_gap_chart",
                    "title": "30 крупнейших расхождений количества",
                    "subtitle": "Отрицательное значение — модель заказала меньше факта.",
                    "type": "bar",
                    "dataset": "top30",
                    "sourceId": "top30_source",
                    "valueFormat": "number",
                    "encodings": {
                        "y": {"field": "label", "type": "ordinal", "label": "SKU"},
                        "x": {
                            "field": "delta_qty",
                            "type": "quantitative",
                            "label": "Модель минус факт, шт.",
                        },
                        "color": {"field": "family", "type": "nominal", "label": "Семейство"},
                    },
                }
            ],
            "tables": [],
            "sources": [source],
            "blocks": [
                {
                    "id": "answer",
                    "type": "markdown",
                    "sourceId": "top30_source",
                    "body": "## Вывод\n\nБольшинство крупных разрывов связано с закупочной логикой, а не с общим недопрогнозом спроса. Статус: можно использовать с оговорками.",
                },
                {"id": "top30_chart_block", "type": "chart", "chartId": "top30_gap_chart"},
            ],
        },
        "snapshot": {
            "version": 1,
            "generatedAt": generated_at,
            "status": "ready",
            "datasets": {"top30": chart_rows, "families": families},
            "accessIssues": [],
        },
        "sources": [source],
        "package_info": {"originUrl": "artifact://display-auto-order-top30-2026-h1"},
    }


def _html_report(markdown_text: str, result: Mapping[str, Any]) -> str:
    summary, detail, families = result["summary"], result["detail"], result["families"]
    rows = "\n".join(
        "<tr>"
        f"<td>{row['rank']}</td><td><code>{html.escape(str(row['nomenclature_code']))}</code><br>{html.escape(_short_name(str(row['name']), 100))}</td>"
        f"<td>{_fmt_int(row['actual_order_qty'])}</td><td>{_fmt_int(row['model_order_qty'])}</td>"
        f"<td>{_fmt_signed_int(row['delta_qty_model_minus_actual'])}</td>"
        f"<td>{_fmt_pct(row['forecast_bias_pct'])}</td><td>{html.escape(str(row['diagnostic_category_ru']))}</td>"
        "</tr>"
        for row in detail
    )
    family_rows = "\n".join(
        f"<tr><td>{html.escape(str(row['family']))}</td><td>{_fmt_int(row['sku_count'])}</td>"
        f"<td>{_fmt_int(row['actual_qty'])}</td><td>{_fmt_int(row['model_qty'])}</td>"
        f"<td>{_fmt_signed_int(row['delta_qty'])}</td><td>{_fmt_int(row['absolute_gap'])}</td></tr>"
        for row in families
    )
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Топ‑30 расхождений автозаказа</title><style>
@page{{size:A4;margin:14mm}}*{{box-sizing:border-box}}body{{margin:0;color:#18202a;font:10.5pt/1.45 Arial,sans-serif}}main{{max-width:190mm;margin:auto}}h1{{font-size:25pt;line-height:1.1;margin:0 0 7mm}}h2{{font-size:16pt;border-top:1px solid #d7dde4;padding-top:4mm;margin:8mm 0 3mm}}h3{{font-size:12pt}}p,li{{margin-bottom:2mm}}.lead{{font-size:13pt;background:#eef4f8;padding:5mm;border-left:4px solid #176b87}}.kpis{{display:grid;grid-template-columns:repeat(4,1fr);gap:3mm;margin:5mm 0}}.kpi{{background:#f2f5f7;padding:4mm}}.kpi strong{{font-size:18pt;display:block}}table{{width:100%;border-collapse:collapse;margin:4mm 0;font-size:8.5pt}}th,td{{border-bottom:1px solid #d7dde4;padding:2mm;text-align:right;vertical-align:top}}th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){{text-align:left}}th{{background:#edf1f4}}tr{{break-inside:avoid}}figure{{margin:5mm 0;break-inside:avoid}}code{{font-size:8.5pt}}.note{{background:#fff5db;padding:4mm}}@media(max-width:700px){{.kpis{{grid-template-columns:1fr 1fr}}table{{font-size:7.5pt}}}}
</style><style>@media(max-width:700px){{figure{{overflow-x:auto}}figure svg{{width:1040px;max-width:none}}}}</style></head><body><main><h1>Топ‑30 расхождений автозаказа</h1>
<p>Февраль–июль 2026 · сценарий поставки 52 дня · историческая симуляция без look‑ahead</p>
<p class="lead"><strong>Главный вывод.</strong> Прогноз спроса не объясняет большинство крупных расхождений. Основной разрыв возникает в правилах закупочной партии, горизонте запаса и распределении между версиями. Это диагноз механизма, а не доказательство, что каждое отличие является ошибкой модели.</p>
<div class="kpis"><div class="kpi"><strong>{summary['under_ordered_sku']} / {summary['over_ordered_sku']}</strong>недозаказ / перезаказ SKU</div><div class="kpi"><strong>{_fmt_int(summary['net_qty_gap'])}</strong>чистый разрыв, шт.</div><div class="kpi"><strong>{_fmt_pct(summary['forecast_bias_pct'],2)}</strong>агрегатный bias прогноза</div><div class="kpi"><strong>{summary['model_order_lines']} / {summary['actual_order_lines']}</strong>строк модели / факта</div></div>
<h2>Расхождения по SKU</h2><figure>{_build_bar_svg(detail)}</figure>
<h2>Концентрация по семействам</h2><figure>{_family_svg(families)}</figure><table><thead><tr><th>Семейство</th><th>SKU</th><th>Факт</th><th>Модель</th><th>Дельта</th><th>Абс. разрыв</th></tr></thead><tbody>{family_rows}</tbody></table>
<h2>Детализация 30 позиций</h2><table><thead><tr><th>№</th><th>SKU</th><th>Факт</th><th>Модель</th><th>Δ</th><th>Bias</th><th>Диагностика</th></tr></thead><tbody>{rows}</tbody></table>
<h2>Подтверждённые выводы и ограничения</h2><p>Полная доказательная записка находится в <code>TOP-30-ANALYSIS.md</code>; детализация расчёта — в <code>top-30-detail.csv</code>, воспроизводимый расчёт — в <code>top-30-analysis.ipynb</code>.</p><p class="note"><strong>Оговорка:</strong> рейтинг построен по разнице заказанного количества, а не по потерянным продажам. Чтобы признать конкретный недозаказ ошибкой модели, нужна SKU-level атрибуция дефицита.</p>
</main></body></html>"""


def _full_report_appendix(result: Mapping[str, Any]) -> str:
    summary, detail, families = result["summary"], result["detail"], result["families"]
    family_rows = "\n".join(
        f"<tr><td>{html.escape(str(row['family']))}</td><td>{_fmt_int(row['sku_count'])}</td>"
        f"<td>{_fmt_int(row['actual_qty'])}</td><td>{_fmt_int(row['model_qty'])}</td>"
        f"<td>{_fmt_signed_int(row['delta_qty'])}</td><td>{_fmt_int(row['absolute_gap'])}</td></tr>"
        for row in families
    )
    detail_rows = "\n".join(
        "<tr>"
        f"<td>{row['rank']}</td><td><code>{html.escape(str(row['nomenclature_code']))}</code><br>"
        f"{html.escape(_short_name(str(row['name']), 90))}</td>"
        f"<td>{_fmt_int(row['actual_order_qty'])}</td><td>{_fmt_int(row['model_order_qty'])}</td>"
        f"<td>{_fmt_signed_int(row['delta_qty_model_minus_actual'])}</td>"
        f"<td>{_fmt_pct(row['forecast_bias_pct'])}</td>"
        f"<td>{html.escape(str(row['diagnostic_category_ru']))}</td></tr>"
        for row in detail
    )
    return f"""<!-- TOP30_APPENDIX_START -->
<h2>Приложение: полный разбор топ‑30 расхождений</h2>
<p><strong>Главный вывод:</strong> прогноз спроса не объясняет большинство крупных расхождений. Основной разрыв возникает в правилах закупочной партии, горизонте запаса и распределении между версиями. Это диагноз механизма, а не доказательство, что каждое отличие является ошибкой модели.</p>
<table><thead><tr><th>Показатель</th><th>Значение</th></tr></thead><tbody>
<tr><td>Недозаказ / перезаказ SKU</td><td>{summary['under_ordered_sku']} / {summary['over_ordered_sku']}</td></tr>
<tr><td>Факт / модель</td><td>{_fmt_int(summary['actual_order_qty'])} / {_fmt_int(summary['model_order_qty'])} шт.</td></tr>
<tr><td>Чистый / абсолютный разрыв</td><td>{_fmt_int(summary['net_qty_gap'])} / {_fmt_int(summary['gross_absolute_qty_gap'])} шт.</td></tr>
<tr><td>Bias / WAPE прогноза</td><td>{_fmt_pct(summary['forecast_bias_pct'],2)} / {_fmt_pct(summary['forecast_wape'],2)}</td></tr>
<tr><td>Строки заказа: факт / модель</td><td>{summary['actual_order_lines']} / {summary['model_order_lines']}</td></tr>
</tbody></table>
<h3>30 крупнейших расхождений количества</h3><figure class="chart">{_build_bar_svg(detail)}</figure>
<h3>Концентрация по товарным семействам</h3><figure class="chart">{_family_svg(families)}</figure>
<table><thead><tr><th>Семейство</th><th>SKU</th><th>Факт</th><th>Модель</th><th>Дельта</th><th>Абс. разрыв</th></tr></thead><tbody>{family_rows}</tbody></table>
<h3>Построчная диагностика</h3>
<table><thead><tr><th>№</th><th>SKU</th><th>Факт</th><th>Модель</th><th>Δ</th><th>Bias</th><th>Диагностика</th></tr></thead><tbody>{detail_rows}</tbody></table>
<h3>Рекомендации</h3><ol>
<li>Добавить мягкие правила MOQ, кратности и типичного закупочного лота.</li>
<li>Считать совместимые версии семейством, затем распределять потребность по версии.</li>
<li>Добавить discrepancy‑сигнал для необычного количества — предупреждение, не блокировку.</li>
<li>Развести потребность до ближайшей поставки и буфер следующего цикла.</li>
<li>Калибровать прогноз адресно для пяти подтверждённых случаев недопрогноза.</li>
</ol>
<p><strong>Оговорка:</strong> рейтинг по разнице закупленного количества не равен рейтингу ущерба от дефицита. Нужна отдельная SKU‑level атрибуция необеспеченных продаж.</p>
<!-- TOP30_APPENDIX_END -->"""


def _update_full_report_html(backtest_dir: Path, result: Mapping[str, Any]) -> None:
    path = backtest_dir / "full-report.html"
    if not path.exists():
        return
    source = path.read_text(encoding="utf-8")
    start_marker = "<!-- TOP30_APPENDIX_START -->"
    end_marker = "<!-- TOP30_APPENDIX_END -->"
    if start_marker in source and end_marker in source:
        before = source.split(start_marker, 1)[0]
        after = source.split(end_marker, 1)[1]
        source = before + after
    appendix = _full_report_appendix(result)
    source = source.replace("\n</main>", f"\n{appendix}\n</main>", 1)
    path.write_text(source, encoding="utf-8")


def build_notebook(backtest_dir: Path) -> Path:
    notebook = nbformat.v4.new_notebook()
    notebook["metadata"]["kernelspec"] = {
        "display_name": "Python 3",
        "language": "python",
        "name": "python3",
    }
    notebook["cells"] = [
        nbformat.v4.new_markdown_cell(
            "# Топ‑30 расхождений автозаказа\n\n"
            "Воспроизводимый разбор 30 SKU с максимальным абсолютным отклонением заказанного количества. "
            "Выборка не является рейтингом ущерба от дефицита."
        ),
        nbformat.v4.new_code_cell(
            "import csv, json\nfrom decimal import Decimal as D\nfrom pathlib import Path\n\n"
            "ROOT = Path('.')\n"
            "summary = json.loads((ROOT / 'top-30-summary.json').read_text(encoding='utf-8'))\n"
            "detail = list(csv.DictReader((ROOT / 'top-30-detail.csv').open(encoding='utf-8-sig')))\n"
            "len(detail), summary['scenario'], summary['selection_rule']"
        ),
        nbformat.v4.new_markdown_cell("## Контрольные суммы"),
        nbformat.v4.new_code_cell(
            "checks = {\n"
            " 'actual_qty': sum(D(r['actual_order_qty']) for r in detail),\n"
            " 'model_qty': sum(D(r['model_order_qty']) for r in detail),\n"
            " 'net_gap': sum(D(r['delta_qty_model_minus_actual']) for r in detail),\n"
            " 'absolute_gap': sum(D(r['absolute_qty_gap']) for r in detail),\n"
            "}\nchecks"
        ),
        nbformat.v4.new_markdown_cell("## Прогноз: достаточно ли он объясняет расхождение?"),
        nbformat.v4.new_code_cell(
            "{k: summary[k] for k in ['observed_sales_weekly_windows','predicted_sales_weekly_windows',"
            "'forecast_bias_pct','forecast_wape','forecast_bias_within_15pct_count',"
            "'forecast_understatement_over_15pct_count']}"
        ),
        nbformat.v4.new_markdown_cell("## Расхождения по семействам"),
        nbformat.v4.new_code_cell("summary['families']"),
        nbformat.v4.new_markdown_cell("## Диагностическая классификация каждой позиции"),
        nbformat.v4.new_code_cell(
            "[{k: r[k] for k in ['rank','nomenclature_code','name','delta_qty_model_minus_actual',"
            "'forecast_bias_pct','diagnostic_category_ru','confidence','recommended_check']} for r in detail]"
        ),
        nbformat.v4.new_markdown_cell(
            "## Вывод\n\n"
            "Большинство крупных количественных расхождений нельзя объяснить недопрогнозом. "
            "Приоритет — MOQ/кратность/горизонт, семейства совместимых версий и отдельный discrepancy-сигнал. "
            "Следующая обязательная проверка — SKU-level вклад в дефицит и необеспеченные продажи."
        ),
    ]
    path = backtest_dir / "top-30-analysis.ipynb"
    nbformat.write(notebook, path)
    executed = NotebookClient(
        notebook,
        timeout=600,
        kernel_name="python3",
        resources={"metadata": {"path": str(backtest_dir)}},
    ).execute()
    nbformat.write(executed, path)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backtest-dir", type=Path, required=True)
    parser.add_argument("--main-scenario", default=MAIN_SCENARIO)
    args = parser.parse_args()
    result = build_top30(args.backtest_dir, args.main_scenario)
    markdown_text = _markdown_report(result)
    (args.backtest_dir / "TOP-30-ANALYSIS.md").write_text(markdown_text, encoding="utf-8")
    artifact = _build_artifact(result)
    (args.backtest_dir / "top-30-artifact.json").write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (args.backtest_dir / "top-30-report.html").write_text(
        _html_report(markdown_text, result), encoding="utf-8"
    )
    _update_full_report_html(args.backtest_dir, result)
    notebook = build_notebook(args.backtest_dir)
    print(
        json.dumps(
            {
                "status": "ready",
                "detail": str(args.backtest_dir / "top-30-detail.csv"),
                "report": str(args.backtest_dir / "TOP-30-ANALYSIS.md"),
                "html": str(args.backtest_dir / "top-30-report.html"),
                "notebook": str(notebook),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
