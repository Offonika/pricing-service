"""Build durable analytical tables and an executed notebook for the backtest."""

from __future__ import annotations

import argparse
import csv
import json
import os
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

import nbformat
from nbclient import NotebookClient

from app.core.config import get_settings
from app.infrastructure.db.engines import build_engine
from tasks.report_display_auto_order_six_month_backtest import load_backtest_items

ZERO = Decimal("0")


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0").strip() or "0")
    except (ArithmeticError, ValueError):
        return ZERO


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8-sig")
        return
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _pct(delta: Decimal, base: Decimal) -> Decimal:
    return delta / base if base else ZERO


def build_analysis(
    *,
    backtest_dir: Path,
    actual_detail_csv: Path,
    cohort_items: Sequence[Mapping[str, Any]],
    main_scenario: str,
) -> dict[str, Any]:
    summary = json.loads((backtest_dir / "summary.json").read_text(encoding="utf-8"))
    decision_rows = _read_csv(backtest_dir / "decision-detail.csv")
    monthly_rows = _read_csv(backtest_dir / "monthly-summary.csv")
    actual_rows = _read_csv(actual_detail_csv)

    item_by_code = {
        str(item.get("nomenclature_code") or "").strip(): item
        for item in cohort_items
        if str(item.get("nomenclature_code") or "").strip()
    }
    cohort_codes = set(item_by_code)
    cohort_rows = [
        {
            "nomenclature_code": code,
            "name": str(item.get("name") or ""),
            "status_current": str(item.get("status") or ""),
            "auto_order_allowed_current": int(bool(item.get("auto_order_allowed"))),
        }
        for code, item in sorted(item_by_code.items())
    ]
    _write_csv(backtest_dir / "cohort.csv", cohort_rows)

    actual_by_code: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"qty": ZERO, "value_rub": ZERO, "lines": 0, "orders": set()}
    )
    actual_by_month: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"qty": ZERO, "value_rub": ZERO, "lines": 0, "orders": set()}
    )
    for row in actual_rows:
        code = str(row.get("nomenclature_code") or "").strip()
        created = str(row.get("supplier_order_created_at") or "")[:10]
        if code not in cohort_codes or not ("2026-02-01" <= created <= "2026-07-31"):
            continue
        qty = _decimal(row.get("qty"))
        value = _decimal(row.get("amount"))
        order_ref = str(row.get("supplier_order_ref") or "")
        month = created[:7]
        actual_by_code[code]["qty"] += qty
        actual_by_code[code]["value_rub"] += value
        actual_by_code[code]["lines"] += 1
        actual_by_code[code]["orders"].add(order_ref)
        actual_by_month[month]["qty"] += qty
        actual_by_month[month]["value_rub"] += value
        actual_by_month[month]["lines"] += 1
        actual_by_month[month]["orders"].add(order_ref)

    model_by_code: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "qty": ZERO,
            "value_rub": ZERO,
            "lines": 0,
            "review_lines": 0,
            "supplier": "",
        }
    )
    review_codes: set[str] = set()
    for row in decision_rows:
        if row.get("scenario") != main_scenario:
            continue
        code = str(row.get("nomenclature_code") or "").strip()
        scheduled = _decimal(row.get("scheduled_order_qty"))
        if scheduled > ZERO:
            model_by_code[code]["qty"] += scheduled
            model_by_code[code]["value_rub"] += _decimal(row.get("recommended_value_rub"))
            model_by_code[code]["lines"] += 1
            model_by_code[code]["supplier"] = str(row.get("supplier_as_of") or "")
        if (
            row.get("decision") == "manual_review"
            and _decimal(row.get("recommended_order_qty")) > ZERO
        ):
            model_by_code[code]["review_lines"] += 1
            review_codes.add(code)

    comparison_rows = []
    for code, item in sorted(item_by_code.items()):
        actual = actual_by_code[code]
        model = model_by_code[code]
        delta_qty = model["qty"] - actual["qty"]
        delta_value = model["value_rub"] - actual["value_rub"]
        comparison_rows.append(
            {
                "nomenclature_code": code,
                "name": str(item.get("name") or ""),
                "status_current": str(item.get("status") or ""),
                "actual_order_qty": str(actual["qty"]),
                "model_order_qty": str(model["qty"]),
                "delta_qty_model_minus_actual": str(delta_qty),
                "actual_order_value_rub": str(actual["value_rub"]),
                "model_order_value_rub": str(model["value_rub"]),
                "delta_value_rub_model_minus_actual": str(delta_value),
                "actual_order_lines": actual["lines"],
                "model_order_lines": model["lines"],
                "manual_review_occurrences": model["review_lines"],
                "supplier_as_of": model["supplier"],
            }
        )
    comparison_rows.sort(
        key=lambda row: abs(_decimal(row["delta_qty_model_minus_actual"])), reverse=True
    )
    _write_csv(backtest_dir / "sku-comparison.csv", comparison_rows)

    actual_purchase_rows = [
        {
            "nomenclature_code": code,
            "name": str(item_by_code[code].get("name") or ""),
            "actual_order_qty": str(values["qty"]),
            "actual_order_value_rub": str(values["value_rub"]),
            "actual_order_lines": values["lines"],
            "actual_order_count": len(values["orders"]),
        }
        for code, values in sorted(actual_by_code.items())
    ]
    _write_csv(backtest_dir / "actual-purchase-by-sku.csv", actual_purchase_rows)

    main_monthly = {
        row["month"]: row for row in monthly_rows if row.get("scenario") == main_scenario
    }
    monthly_comparison = []
    for month in [f"2026-{value:02d}" for value in range(2, 8)]:
        model = main_monthly[month]
        actual = actual_by_month[month]
        monthly_comparison.append(
            {
                "month": month,
                "actual_order_qty": str(actual["qty"]),
                "model_order_qty": model["ordered_qty"],
                "delta_qty_model_minus_actual": str(_decimal(model["ordered_qty"]) - actual["qty"]),
                "actual_order_value_rub": str(actual["value_rub"]),
                "model_order_value_rub": model["ordered_value_rub"],
                "actual_sales_qty": model["actual_sales_qty"],
                "actual_order_count": len(actual["orders"]),
                "model_project_count": len(
                    {
                        (row["decision_date"], row["supplier_as_of"])
                        for row in decision_rows
                        if row.get("scenario") == main_scenario
                        and row.get("month") == month
                        and _decimal(row.get("scheduled_order_qty")) > ZERO
                    }
                ),
                "model_stockout_sku_days": model["model_stockout_sku_days"],
                "model_unmet_observed_sales_qty": model["model_unmet_observed_sales_qty"],
            }
        )
    _write_csv(backtest_dir / "monthly-comparison.csv", monthly_comparison)

    main = next(row for row in summary["scenarios"] if row["scenario"] == main_scenario)
    actual = summary["actual_supplier_orders"]
    actual_stock = summary["actual_stock"]
    actual_qty = _decimal(actual["qty"])
    model_qty = _decimal(main["ordered_qty"])
    actual_value = _decimal(actual["value_rub"])
    model_value = _decimal(main["ordered_value_rub"])
    forecast_actual = _decimal(main["forecast_actual_qty_7d"])
    forecast_predicted = _decimal(main["forecast_predicted_qty_7d"])
    forecast_abs_error = _decimal(main["forecast_abs_error_qty_7d"])
    demand_active_days = Decimal(actual_stock["demand_active_sku_days"])
    analysis = {
        "schema": "display_auto_order_backtest_analysis.v1",
        "main_scenario": main_scenario,
        "actual_order_qty": str(actual_qty),
        "model_order_qty": str(model_qty),
        "order_qty_delta": str(model_qty - actual_qty),
        "order_qty_delta_pct": str(_pct(model_qty - actual_qty, actual_qty)),
        "actual_order_value_rub": str(actual_value),
        "model_order_value_rub": str(model_value),
        "order_value_delta_rub": str(model_value - actual_value),
        "order_value_delta_pct": str(_pct(model_value - actual_value, actual_value)),
        "forecast_bias_pct": str(_pct(forecast_predicted - forecast_actual, forecast_actual)),
        "forecast_wape": str(_pct(forecast_abs_error, forecast_actual)),
        "actual_stockout_share": actual_stock["stockout_share"],
        "model_stockout_share": str(Decimal(main["model_stockout_sku_days"]) / demand_active_days),
        "stockout_sku_days_delta": int(main["model_stockout_sku_days"])
        - int(actual_stock["stockout_sku_days"]),
        "actual_ending_stock_qty": actual_stock["ending_stock_qty"],
        "model_ending_stock_qty": main["ending_stock_qty"],
        "ending_stock_delta_qty": str(
            _decimal(main["ending_stock_qty"]) - _decimal(actual_stock["ending_stock_qty"])
        ),
        "priced_order_qty_share": str(_pct(_decimal(main["priced_order_qty"]), model_qty)),
        "manual_review_distinct_sku": len(review_codes),
        "manual_review_occurrences": int(main["manual_review_lines"]),
        "top_under_ordered": [
            row
            for row in sorted(
                comparison_rows,
                key=lambda item: _decimal(item["delta_qty_model_minus_actual"]),
            )[:15]
        ],
        "top_over_ordered": [
            row
            for row in sorted(
                comparison_rows,
                key=lambda item: _decimal(item["delta_qty_model_minus_actual"]),
                reverse=True,
            )[:15]
        ],
        "monthly_comparison": monthly_comparison,
        "scenario_summary": summary["scenarios"],
        "validation_status": "share_with_caveats",
    }
    (backtest_dir / "analysis-summary.json").write_text(
        json.dumps(analysis, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return analysis


def build_notebook(backtest_dir: Path, analysis: Mapping[str, Any]) -> Path:
    def pct(value: Any) -> str:
        return f"{_decimal(value) * Decimal('100'):.1f}%"

    notebook = nbformat.v4.new_notebook()
    notebook["metadata"]["kernelspec"] = {
        "display_name": "Python 3",
        "language": "python",
        "name": "python3",
    }
    notebook["cells"] = [
        nbformat.v4.new_markdown_cell(
            "## tl;dr\n\n"
            f"- Основной сценарий (52 дня) предлагает **{analysis['model_order_qty']} шт.** "
            f"против фактических **{analysis['actual_order_qty']} шт.** "
            f"({pct(analysis['order_qty_delta_pct'])}).\n"
            f"- Сумма модели **{_decimal(analysis['model_order_value_rub']) / Decimal('1000000'):.2f} млн ₽**, "
            f"факт **{_decimal(analysis['actual_order_value_rub']) / Decimal('1000000'):.2f} млн ₽**.\n"
            f"- Агрегатный прогноз спроса смещён на {pct(analysis['forecast_bias_pct'])}, "
            f"но SKU-level WAPE равен {pct(analysis['forecast_wape'])}; автоматическое принятие "
            "всех строк без закупщика пока не подтверждено."
        ),
        nbformat.v4.new_markdown_cell(
            "## Context & Methods\n\n"
            "### Key Assumptions\n\n"
            "Период оценки: 1 февраля — 31 июля 2026 года; warm-up начинается 24 июля 2025 года. "
            "Решения принимаются раз в 7 дней без look-ahead. Исторический склад и товар в пути "
            "восстановлены из регистров 1С. Прошлые резервы, исторические статусы и quality-blockers "
            "не восстановлены полностью, поэтому результат имеет статус Share with caveats."
        ),
        nbformat.v4.new_markdown_cell("## Data\n\n### 1. Load generated artifacts"),
        nbformat.v4.new_code_cell(
            "import csv, json\n"
            "from pathlib import Path\n\n"
            "ROOT = Path('.')\n"
            "analysis = json.loads((ROOT / 'analysis-summary.json').read_text(encoding='utf-8'))\n"
            "monthly = list(csv.DictReader((ROOT / 'monthly-comparison.csv').open(encoding='utf-8-sig')))\n"
            "comparison = list(csv.DictReader((ROOT / 'sku-comparison.csv').open(encoding='utf-8-sig')))\n"
            "{'months': len(monthly), 'sku_rows': len(comparison), 'scenario': analysis['main_scenario']}"
        ),
        nbformat.v4.new_markdown_cell("## Results\n\n### 2. Monthly fact-versus-model comparison"),
        nbformat.v4.new_code_cell("monthly"),
        nbformat.v4.new_markdown_cell("### 3. Largest quantity differences by SKU"),
        nbformat.v4.new_code_cell(
            "{'under_ordered': analysis['top_under_ordered'][:10], "
            "'over_ordered': analysis['top_over_ordered'][:10]}"
        ),
        nbformat.v4.new_markdown_cell("### 4. Validation metrics"),
        nbformat.v4.new_code_cell(
            "{key: analysis[key] for key in [\n"
            "    'forecast_bias_pct', 'forecast_wape', 'actual_stockout_share',\n"
            "    'model_stockout_share', 'priced_order_qty_share',\n"
            "    'manual_review_distinct_sku', 'validation_status'\n"
            "]}"
        ),
        nbformat.v4.new_markdown_cell(
            "## Takeaways\n\n"
            "1. Формула близка к факту по общему количеству, но заметно меняет товарный и ценовой микс.\n"
            "2. Небольшая агрегатная ошибка спроса скрывает большую SKU-level ошибку; ручная проверка остаётся необходимой.\n"
            "3. Следующий прирост доверия даст восстановление исторических резервов, статусов и quality-blockers, затем повторная сверка по решениям закупщика."
        ),
    ]
    path = backtest_dir / "analysis.ipynb"
    nbformat.write(notebook, path)
    client = NotebookClient(
        notebook,
        timeout=600,
        kernel_name="python3",
        resources={"metadata": {"path": str(backtest_dir)}},
    )
    executed = client.execute()
    nbformat.write(executed, path)
    return path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build display auto-order backtest analysis")
    parser.add_argument("--backtest-dir", type=Path, required=True)
    parser.add_argument("--actual-detail-csv", type=Path, required=True)
    parser.add_argument("--database-url", default="")
    parser.add_argument("--folder", default="дисплеи")
    parser.add_argument("--main-scenario", default="lead_time_52d")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    settings = get_settings()
    app_url = args.database_url or os.environ.get("DATABASE_URL") or settings.database_url
    engine = build_engine(app_url, pool_pre_ping=True)
    try:
        cohort_items, _run_id = load_backtest_items(engine, folder=args.folder)
    finally:
        engine.dispose()
    analysis = build_analysis(
        backtest_dir=args.backtest_dir,
        actual_detail_csv=args.actual_detail_csv,
        cohort_items=cohort_items,
        main_scenario=args.main_scenario,
    )
    notebook_path = build_notebook(args.backtest_dir, analysis)
    print(
        json.dumps(
            {
                "status": "ready",
                "analysis_summary": str(args.backtest_dir / "analysis-summary.json"),
                "notebook": str(notebook_path),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
