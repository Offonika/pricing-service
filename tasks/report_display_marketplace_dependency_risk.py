from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence

DEFAULT_REPORT_ROOT = Path("reports/assortment_lifecycle")

CSV_COLUMNS = [
    "risk_code",
    "risk_ru",
    "dependency_code",
    "dependency_ru",
    "action_ru",
    "nomenclature_code",
    "name",
    "status_label",
    "speed_tier",
    "total_net_sales_qty",
    "non_marketplace_net_sales_qty",
    "marketplace_net_sales_qty",
    "marketplace_share_pct",
    "sales_doc_count_total",
    "sales_doc_count_non_marketplace",
    "sales_doc_count_marketplace",
    "last_sale_at",
    "last_non_marketplace_sale_at",
    "last_marketplace_sale_at",
    "current_recommended_order_qty",
    "regular_only_order_qty_estimate",
    "order_gap_qty",
    "current_free_stock_qty",
    "incoming_qty",
    "stock_exposure_qty",
    "physical_effective_free_qty_gt_2",
    "physical_low_residue_qty_1_2",
    "site_online_free_qty",
    "wholesale_free_qty",
    "central_free_qty_info",
    "dry_run_decision",
    "reason_ru",
]

RISK_RANK = {
    "critical_marketplace_refusal_nonliquid_risk": 0,
    "high_marketplace_refusal_risk": 1,
    "medium_channel_split_required": 2,
    "watch_order_impact": 3,
    "low_marketplace_dependency": 4,
}

MANUAL_RISK_CODES = {
    "critical_marketplace_refusal_nonliquid_risk",
    "high_marketplace_refusal_risk",
    "medium_channel_split_required",
    "watch_order_impact",
}

MEDIUM_MIN_MARKETPLACE_QTY = Decimal("7")
WATCH_MIN_ORDER_GAP_QTY = Decimal("10")

RISK_LABELS_RU = {
    "critical_marketplace_refusal_nonliquid_risk": (
        "Критический: риск неликвида при отказе маркетплейсщика"
    ),
    "high_marketplace_refusal_risk": "Высокий: маркетплейс больше половины спроса",
    "medium_channel_split_required": "Средний: нужен разрез магазин/маркетплейс",
    "watch_order_impact": "Контроль: маркетплейс влияет на заказ",
    "low_marketplace_dependency": "Низкий: маркетплейс не главный риск",
}

DEPENDENCY_LABELS_RU = {
    "marketplace_only": "Только маркетплейс",
    "marketplace_dominates_70_plus": "Маркетплейс доминирует 70%+",
    "marketplace_majority_50_70": "Маркетплейс больше половины 50-70%",
    "marketplace_significant_30_50": "Маркетплейс заметный 30-50%",
    "marketplace_watch_10_30": "Маркетплейс влияет 10-30%",
    "marketplace_low": "Маркетплейс не главный",
}

ACTION_RU = {
    "critical_marketplace_refusal_nonliquid_risk": (
        "Не заказывать автоматически; проверить обычный спрос и остаток; " "решение только вручную."
    ),
    "high_marketplace_refusal_risk": (
        "Отделить магазинную потребность от маркетплейсной; автозаказ только " "после проверки."
    ),
    "medium_channel_split_required": (
        "Показывать две потребности; при заказе проверить, что обычный спрос "
        "покрывает количество."
    ),
    "watch_order_impact": "Сравнить заказ по всему спросу и без маркетплейса.",
    "low_marketplace_dependency": "Долю маркетплейса показывать справочно.",
}


def main() -> int:
    args = _parse_args()
    slice_rows = read_csv(args.counterparty_slices_csv)
    stock_rows = read_csv(args.stock_risk_csv)
    risk_rows = build_risk_rows(slice_rows, stock_rows)
    summary = build_summary(
        risk_rows,
        as_of=args.as_of,
        counterparty_slices_csv=args.counterparty_slices_csv,
        stock_risk_csv=args.stock_risk_csv,
    )
    write_csv(args.output_csv, risk_rows, CSV_COLUMNS)
    write_markdown(args.output_md, risk_rows, summary)
    payload = {
        "status": "ready",
        "output_csv": str(args.output_csv),
        "output_md": str(args.output_md),
        **summary,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def build_risk_rows(
    slice_rows: Sequence[Mapping[str, Any]],
    stock_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    stock_by_code = {_clean(row.get("nomenclature_code")): row for row in stock_rows}
    result: list[dict[str, Any]] = []
    for row in slice_rows:
        code = _clean(row.get("nomenclature_code"))
        stock_row = stock_by_code.get(code, {})
        share = _decimal(row.get("marketplace_share_pct")) or Decimal("0")
        non_marketplace_qty = _decimal(row.get("non_marketplace_net_sales_qty")) or Decimal("0")
        marketplace_qty = _decimal(row.get("marketplace_net_sales_qty")) or Decimal("0")
        if marketplace_qty <= 0:
            continue
        current_order = _decimal(row.get("current_recommended_order_qty")) or Decimal("0")
        regular_order = _decimal(row.get("regular_only_order_qty_estimate")) or Decimal("0")
        free_stock = _first_decimal(
            stock_row.get("current_free_stock_qty"),
            row.get("free_stock_qty"),
        )
        incoming = _first_decimal(row.get("incoming_qty"), stock_row.get("incoming_qty"))
        stock_exposure = free_stock + incoming + current_order
        risk_code = choose_risk_code(
            non_marketplace_qty=non_marketplace_qty,
            marketplace_qty=marketplace_qty,
            marketplace_share_pct=share,
            current_order=current_order,
            regular_only_order=regular_order,
            stock_exposure=stock_exposure,
        )
        dependency_code = choose_dependency_code(
            non_marketplace_qty=non_marketplace_qty,
            marketplace_share_pct=share,
        )
        result.append(
            {
                "risk_code": risk_code,
                "risk_ru": RISK_LABELS_RU[risk_code],
                "dependency_code": dependency_code,
                "dependency_ru": DEPENDENCY_LABELS_RU[dependency_code],
                "action_ru": ACTION_RU[risk_code],
                "nomenclature_code": code,
                "name": _clean(row.get("name")),
                "status_label": _clean(row.get("status_label")),
                "speed_tier": _clean(row.get("speed_tier")),
                "total_net_sales_qty": _out_decimal(
                    _decimal(row.get("total_net_sales_qty")) or Decimal("0")
                ),
                "non_marketplace_net_sales_qty": _out_decimal(non_marketplace_qty),
                "marketplace_net_sales_qty": _out_decimal(marketplace_qty),
                "marketplace_share_pct": _out_decimal(share),
                "sales_doc_count_total": _clean(row.get("sales_doc_count_total")),
                "sales_doc_count_non_marketplace": _clean(
                    row.get("sales_doc_count_non_marketplace")
                ),
                "sales_doc_count_marketplace": _clean(row.get("sales_doc_count_marketplace")),
                "last_sale_at": _clean(row.get("last_sale_at")),
                "last_non_marketplace_sale_at": _clean(row.get("last_non_marketplace_sale_at")),
                "last_marketplace_sale_at": _clean(row.get("last_marketplace_sale_at")),
                "current_recommended_order_qty": _out_decimal(current_order),
                "regular_only_order_qty_estimate": _out_decimal(regular_order),
                "order_gap_qty": _out_decimal(current_order - regular_order),
                "current_free_stock_qty": _out_decimal(free_stock),
                "incoming_qty": _out_decimal(incoming),
                "stock_exposure_qty": _out_decimal(stock_exposure),
                "physical_effective_free_qty_gt_2": _out_decimal(
                    _decimal(stock_row.get("physical_sales_point_effective_free_qty_excl_1_2"))
                    or Decimal("0")
                ),
                "physical_low_residue_qty_1_2": _out_decimal(
                    _decimal(stock_row.get("physical_low_residue_qty_1_2")) or Decimal("0")
                ),
                "site_online_free_qty": _out_decimal(
                    _decimal(stock_row.get("site_online_free_qty")) or Decimal("0")
                ),
                "wholesale_free_qty": _out_decimal(
                    _decimal(stock_row.get("wholesale_free_qty")) or Decimal("0")
                ),
                "central_free_qty_info": _out_decimal(
                    _decimal(stock_row.get("central_free_qty_info")) or Decimal("0")
                ),
                "dry_run_decision": _clean(row.get("dry_run_decision")),
                "reason_ru": _clean(row.get("reason_ru")),
            }
        )
    return sorted(
        result,
        key=lambda item: (
            RISK_RANK.get(_clean(item.get("risk_code")), 99),
            -(_decimal(item.get("stock_exposure_qty")) or Decimal("0")),
            -(_decimal(item.get("marketplace_share_pct")) or Decimal("0")),
            _clean(item.get("nomenclature_code")),
        ),
    )


def choose_risk_code(
    *,
    non_marketplace_qty: Decimal,
    marketplace_qty: Decimal,
    marketplace_share_pct: Decimal,
    current_order: Decimal,
    regular_only_order: Decimal,
    stock_exposure: Decimal,
) -> str:
    has_exposure = stock_exposure > 0
    if has_exposure and (non_marketplace_qty <= 0 or marketplace_share_pct >= Decimal("70")):
        return "critical_marketplace_refusal_nonliquid_risk"
    if has_exposure and marketplace_share_pct >= Decimal("50"):
        return "high_marketplace_refusal_risk"
    if (
        marketplace_qty >= MEDIUM_MIN_MARKETPLACE_QTY
        and (has_exposure or current_order > 0)
        and marketplace_share_pct >= Decimal("30")
    ):
        return "medium_channel_split_required"
    if (
        current_order > 0
        and marketplace_share_pct >= Decimal("10")
        and current_order - regular_only_order >= WATCH_MIN_ORDER_GAP_QTY
    ):
        return "watch_order_impact"
    return "low_marketplace_dependency"


def choose_dependency_code(
    *,
    non_marketplace_qty: Decimal,
    marketplace_share_pct: Decimal,
) -> str:
    if non_marketplace_qty <= 0:
        return "marketplace_only"
    if marketplace_share_pct >= Decimal("70"):
        return "marketplace_dominates_70_plus"
    if marketplace_share_pct >= Decimal("50"):
        return "marketplace_majority_50_70"
    if marketplace_share_pct >= Decimal("30"):
        return "marketplace_significant_30_50"
    if marketplace_share_pct >= Decimal("10"):
        return "marketplace_watch_10_30"
    return "marketplace_low"


def build_summary(
    rows: Sequence[Mapping[str, Any]],
    *,
    as_of: date,
    counterparty_slices_csv: Path,
    stock_risk_csv: Path,
) -> dict[str, Any]:
    risk_counts = Counter(_clean(row.get("risk_code")) for row in rows)
    dependency_counts = Counter(_clean(row.get("dependency_code")) for row in rows)
    return {
        "schema": "display_auto_order_marketplace_dependency_risk.v1",
        "as_of": as_of.isoformat(),
        "input_counterparty_slices_csv": str(counterparty_slices_csv),
        "input_stock_risk_csv": str(stock_risk_csv),
        "items": len(rows),
        "marketplace_only_rows": dependency_counts["marketplace_only"],
        "marketplace_50_70_rows": dependency_counts["marketplace_majority_50_70"],
        "marketplace_30_50_rows": dependency_counts["marketplace_significant_30_50"],
        "critical_risk_rows": risk_counts["critical_marketplace_refusal_nonliquid_risk"],
        "high_risk_rows": risk_counts["high_marketplace_refusal_risk"],
        "medium_risk_rows": risk_counts["medium_channel_split_required"],
        "watch_order_impact_rows": risk_counts["watch_order_impact"],
        "manual_review_rows": sum(
            count for code, count in risk_counts.items() if code in MANUAL_RISK_CODES
        ),
        "risk_counts": dict(sorted(risk_counts.items())),
        "dependency_counts": dict(sorted(dependency_counts.items())),
    }


def write_markdown(
    path: Path,
    rows: Sequence[Mapping[str, Any]],
    summary: Mapping[str, Any],
) -> Path:
    manual_rows = [row for row in rows if _clean(row.get("risk_code")) in MANUAL_RISK_CODES]
    critical_rows = [
        row
        for row in rows
        if _clean(row.get("risk_code")) == "critical_marketplace_refusal_nonliquid_risk"
    ]
    top_rows = [
        row
        for row in rows
        if _clean(row.get("risk_code"))
        in {"critical_marketplace_refusal_nonliquid_risk", "high_marketplace_refusal_risk"}
    ][:12]
    lines = [
        "# Риск неликвида при зависимости от маркетплейс-покупателей",
        "",
        (
            f"Дата среза: `{summary['as_of']}`. Источник: "
            "`display-auto-order-counterparty-type-slices.csv` + складовой срез "
            "`display-auto-order-sales-point-stock-risk.csv`."
        ),
        "",
        "## Главный вывод",
        "",
        (
            "Самый опасный сценарий - товар покупали только или почти только "
            "маркетплейс-покупатели, а у нас уже есть остаток, товар в пути или "
            "рекомендация к заказу. Если такой покупатель откажется, товар может "
            "стать неликвидом для магазинов."
        ),
        "",
        (
            f"На текущем срезе настоящий `только маркетплейс` найден по "
            f"`{summary['marketplace_only_rows']}` SKU. У него есть складская "
            "экспозиция, поэтому он получает критический риск."
        ),
        "",
        "## Автоматическая метка риска",
        "",
        "| Метка | Условие | Что делать |",
        "| --- | --- | --- |",
        (
            "| `critical_marketplace_refusal_nonliquid_risk` | обычный спрос = 0 или "
            "маркетплейс >=70%, и есть остаток/путь/заказ | Не заказывать "
            "автоматически; только ручное решение |"
        ),
        (
            "| `high_marketplace_refusal_risk` | маркетплейс 50-70% и есть "
            "остаток/путь/заказ | Разделить магазинную и маркетплейсную "
            "потребность |"
        ),
        (
            "| `medium_channel_split_required` | маркетплейс 30-50% и есть "
            "экспозиция или заказ, минимум 7 продаж МП | Показывать две "
            "потребности, проверять количество руками |"
        ),
        (
            "| `watch_order_impact` | маркетплейс 10-30%, но влияет на заказ "
            "минимум на 10 шт. | Сравнить заказ по всему спросу и без "
            "маркетплейса |"
        ),
        "| `low_marketplace_dependency` | маркетплейс не главный риск | Долю показывать справочно |",
        "",
        "## Сводка",
        "",
        "| Метрика | Значение |",
        "| --- | ---: |",
        f"| SKU с маркетплейс-спросом | {summary['items']} |",
        f"| Только маркетплейс | {summary['marketplace_only_rows']} |",
        f"| Маркетплейс 50-70% | {summary['marketplace_50_70_rows']} |",
        f"| Маркетплейс 30-50% | {summary['marketplace_30_50_rows']} |",
        f"| Критический риск | {summary['critical_risk_rows']} |",
        f"| Высокий риск | {summary['high_risk_rows']} |",
        f"| Средний риск | {summary['medium_risk_rows']} |",
        f"| Контроль влияния на заказ | {summary['watch_order_impact_rows']} |",
        f"| Всего строк в ручной разбор по этому правилу | {summary['manual_review_rows']} |",
        "",
        *_critical_singleton_section(critical_rows),
        "",
        "## Самые опасные строки",
        "",
        *_markdown_table(top_rows),
        "",
        "## Все строки, которые надо смотреть руками",
        "",
        *_markdown_table(manual_rows),
        "",
        "## Как использовать в карточке `Потребность`",
        "",
        "В блоке спроса нужно автоматически показывать:",
        "",
        "- `marketplace_share_pct` - доля маркетплейса;",
        "- `non_marketplace_net_sales_qty` - обычный спрос без маркетплейса;",
        "- `marketplace_net_sales_qty` - спрос маркетплейса;",
        "- `marketplace_dependency_risk` - метка риска;",
        (
            "- `stock_exposure_qty` - что может зависнуть: свободный остаток + "
            "товар в пути + текущий рекомендуемый заказ;"
        ),
        "- `action_ru` - что делать закупщику.",
        "",
        (
            "Правило для автозаказа после утверждения владельцем: строки с "
            "`critical` и `high` не должны уходить в обычный автоматический заказ "
            "без ручного решения. Это не изменение боевой формулы на текущем шаге, "
            "а read-only - только для чтения правило на осмотр."
        ),
        "",
        "Полная таблица: `reports/assortment_lifecycle/"
        f"{summary['as_of']}/display-auto-order-marketplace-dependency-risk.csv`.",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _critical_singleton_section(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    if len(rows) != 1:
        return []
    row = rows[0]
    return [
        "## Единичный ручной случай",
        "",
        (
            f"`{_clean(row.get('nomenclature_code'))}` не требует отдельной "
            "автоматизации ради одного SKU. Решение: оставить в ручной проверке "
            "с объяснением."
        ),
        "",
        "Объяснение для карточки:",
        "",
        (
            "- спрос найден только у маркетплейс-покупателя: обычный спрос без "
            f"маркетплейса `{_clean(row.get('non_marketplace_net_sales_qty'))}`;"
        ),
        f"- доля маркетплейса `{_clean(row.get('marketplace_share_pct'))}%`;",
        ("- если маркетплейсщик откажется, товар может зависнуть как " "неликвид для магазинов;"),
        (
            "- автоматический заказ и перевод в магазинную матрицу запрещены "
            "без отдельного ручного решения."
        ),
    ]


def _markdown_table(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    lines = [
        "| Риск | Код | Доля МП % | Всего | Без МП | МП | Остаток+путь+заказ | Заказ | Заказ без МП | Действие | Название |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for row in rows:
        lines.append(
            "| "
            + " | ".join(
                [
                    _md_cell(row.get("risk_ru")),
                    f"`{_md_cell(row.get('nomenclature_code'))}`",
                    _md_cell(row.get("marketplace_share_pct")),
                    _md_cell(row.get("total_net_sales_qty")),
                    _md_cell(row.get("non_marketplace_net_sales_qty")),
                    _md_cell(row.get("marketplace_net_sales_qty")),
                    _md_cell(row.get("stock_exposure_qty")),
                    _md_cell(row.get("current_recommended_order_qty")),
                    _md_cell(row.get("regular_only_order_qty_estimate")),
                    _md_cell(_truncate(row.get("action_ru"), 72)),
                    _md_cell(_truncate(row.get("name"), 96)),
                ]
            )
            + " |"
        )
    return lines


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as csv_file:
        return list(csv.DictReader(csv_file))


def write_csv(path: Path, rows: Sequence[Mapping[str, Any]], columns: Sequence[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in columns})
    return path


def _first_decimal(*values: Any) -> Decimal:
    for value in values:
        parsed = _decimal(value)
        if parsed is not None:
            return parsed
    return Decimal("0")


def _md_cell(value: Any) -> str:
    return _clean(value).replace("|", "\\|")


def _truncate(value: Any, limit: int) -> str:
    text = _clean(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).replace(" ", "").replace(",", "."))
    except (InvalidOperation, ValueError):
        return None


def _out_decimal(value: Decimal) -> str:
    formatted = format(value.normalize(), "f")
    if "." not in formatted:
        return formatted
    return formatted.rstrip("0").rstrip(".") or "0"


def _parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"date must be YYYY-MM-DD, got: {value}") from exc


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=("Build a read-only display demand risk report for marketplace-dependent SKUs.")
    )
    parser.add_argument("--as-of", type=_parse_date, default=date.today())
    parser.add_argument(
        "--counterparty-slices-csv",
        type=Path,
        default=None,
        help="CSV with demand slices by counterparty type.",
    )
    parser.add_argument(
        "--stock-risk-csv",
        type=Path,
        default=None,
        help="CSV with sales-point stock exposure and low-residue risk.",
    )
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--output-md", type=Path, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report_dir = DEFAULT_REPORT_ROOT / args.as_of.isoformat()
    if args.counterparty_slices_csv is None:
        args.counterparty_slices_csv = (
            report_dir / "display-auto-order-counterparty-type-slices.csv"
        )
    if args.stock_risk_csv is None:
        args.stock_risk_csv = report_dir / "display-auto-order-sales-point-stock-risk.csv"
    if args.output_csv is None:
        args.output_csv = report_dir / "display-auto-order-marketplace-dependency-risk.csv"
    if args.output_md is None:
        args.output_md = (
            report_dir
            / f"display-auto-order-marketplace-dependency-risk-review-{args.as_of.isoformat()}.md"
        )
    return args


if __name__ == "__main__":
    raise SystemExit(main())
