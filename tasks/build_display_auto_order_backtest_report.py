"""Build the canonical Data Analytics artifact for the six-month backtest."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value or "0"))


def _number(value: Any) -> float:
    return float(_decimal(value))


def build_artifact(backtest_dir: Path) -> dict[str, Any]:
    analysis = json.loads((backtest_dir / "analysis-summary.json").read_text(encoding="utf-8"))
    raw_summary = json.loads((backtest_dir / "summary.json").read_text(encoding="utf-8"))
    generated_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    demand_days = Decimal(raw_summary["actual_stock"]["demand_active_sku_days"])
    scenario_rows = [
        {
            "scenario": row["scenario"],
            "scenario_label": f"{row['lead_time_days']} дней",
            "lead_time_days": row["lead_time_days"],
            "ordered_qty": _number(row["ordered_qty"]),
            "ordered_value_rub": _number(row["ordered_value_rub"]),
            "stockout_share": float(Decimal(row["model_stockout_sku_days"]) / demand_days),
            "unmet_observed_sales_qty": _number(row["model_unmet_observed_sales_qty"]),
        }
        for row in raw_summary["scenarios"]
    ]
    source = {
        "id": "backtest_source",
        "label": "1С и pricing-service: исторический backtest автозаказа",
        "path": "source-queries.sql",
    }
    charts = [
        {
            "id": "lead_time_chart",
            "title": "Риск дефицита при разных сроках поставки",
            "subtitle": "Факт — 5,6%; модель — 11,0–13,9%.",
            "type": "bar",
            "dataset": "lead_time_scenarios",
            "sourceId": "backtest_source",
            "valueFormat": "percent",
            "encodings": {
                "x": {"field": "scenario_label", "type": "ordinal", "label": "Срок поставки"},
                "y": {"field": "stockout_share", "type": "quantitative", "label": "Доля дефицита"},
                "tooltip": [
                    {"field": "ordered_qty", "type": "quantitative", "label": "Заказ, шт."},
                    {
                        "field": "unmet_observed_sales_qty",
                        "type": "quantitative",
                        "label": "Не обеспечено наблюдаемых продаж, шт.",
                    },
                ],
            },
            "referenceLines": [
                {
                    "axis": "y",
                    "value": _number(analysis["actual_stockout_share"]),
                    "label": "Факт 5,6%",
                    "style": "dashed",
                }
            ],
        },
    ]
    blocks = [
        {
            "id": "executive_summary",
            "type": "markdown",
            "sourceId": "backtest_source",
            "body": (
                "## Вывод\n\n"
                "Модель дала бы **−6,6% штук**, **+25,2% стоимости** и рост дефицита "
                "**с 5,6% до 12,4%**; SKU-level WAPE — **46,2%**. "
                "**Решение:** создавать черновики в помощнике, но проверять их и вручную передавать в 1С. "
                "Автоматическую отправку в 1С пока не включать. "
                "Статус — **с оговорками**: прошлые резервы и исторические блокеры качества "
                "восстановлены не полностью."
            ),
        },
        {"id": "lead_time_chart_block", "type": "chart", "chartId": "lead_time_chart"},
    ]

    return {
        "surface": "report",
        "manifest": {
            "version": 1,
            "surface": "report",
            "title": "Полугодовой backtest автозаказа дисплеев",
            "description": "Факт против закрытой симуляции за февраль–июль 2026 года.",
            "generatedAt": generated_at,
            "filters": [],
            "cards": [],
            "charts": charts,
            "tables": [],
            "sources": [source],
            "blocks": blocks,
        },
        "snapshot": {
            "version": 1,
            "generatedAt": generated_at,
            "status": "ready",
            "datasets": {
                "lead_time_scenarios": scenario_rows,
            },
            "accessIssues": [],
        },
        "sources": [source],
        "package_info": {"originUrl": "artifact://display-auto-order-backtest-2026-h1"},
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build display auto-order backtest report artifact"
    )
    parser.add_argument("--backtest-dir", type=Path, required=True)
    args = parser.parse_args()
    artifact = build_artifact(args.backtest_dir)
    output = args.backtest_dir / "artifact.json"
    output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "ready", "artifact": str(output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
