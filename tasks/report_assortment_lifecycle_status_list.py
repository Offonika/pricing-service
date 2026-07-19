"""Список карточек по статусам (не только сводка) — read-only, ничего не пишет.

Переиспользует ту же логику получения фактов и той же формулы, что
``refresh_assortment_lifecycle_classification.py``, но вместо агрегированной
сводки выводит список каждой карточки: код, название, статус.

Аргументы — тот же набор конфигов, что и у refresh-задачи.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from app.core.config import get_settings
from tasks.build_assortment_lifecycle_updates import build_updates_from_records
from tasks.refresh_assortment_lifecycle_classification import _load_or_build_fact_records


def build_report(args: argparse.Namespace) -> dict:
    settings = get_settings()
    facts = _load_or_build_fact_records(
        args,
        database_url=args.database_url or os.environ.get("DATABASE_URL") or settings.database_url,
        settings_onec_database_url=args.onec_database_url or settings.onec_database_url or "",
    )
    _rows, summaries = build_updates_from_records(facts, folder_filter=args.folder)

    by_status: dict[str, list[dict]] = {}
    for item in summaries:
        by_status.setdefault(item["status_label"], []).append(
            {
                "nomenclature_code": item["nomenclature_code"],
                "name": item["name"],
                "folder": item["folder"],
            }
        )

    return {
        "folder": args.folder,
        "total": len(summaries),
        "counts_by_status": {label: len(items) for label, items in by_status.items()},
        "items_by_status": by_status,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", default=os.getenv("ASSORTMENT_LIFECYCLE_FOLDER", "дисплеи"))
    parser.add_argument(
        "--history-months",
        type=int,
        default=int(os.getenv("ASSORTMENT_LIFECYCLE_HISTORY_MONTHS", "24")),
    )
    parser.add_argument("--today", default=None)
    parser.add_argument(
        "--limit", type=int, default=int(os.getenv("ASSORTMENT_LIFECYCLE_LIMIT", "12000"))
    )
    parser.add_argument("--database-url", default="")
    parser.add_argument("--onec-database-url", default="")
    parser.add_argument("--facts-json", type=Path, default=None)
    parser.add_argument("--source-rows-json", type=Path, default=None)
    parser.add_argument("--warehouse-policy-json", type=Path, required=True)
    parser.add_argument("--supplier-order-mapping-json", type=Path, required=True)
    parser.add_argument("--receipt-mapping-json", type=Path, required=True)
    parser.add_argument("--manual-overrides-json", type=Path, default=None)
    parser.add_argument("--manager-signals-json", type=Path, default=None)
    parser.add_argument("--fact-status-decisions-json", type=Path, default=None)
    parser.add_argument("--output-json", type=Path, required=True)
    args = parser.parse_args()
    if args.today is not None:
        from datetime import date

        args.today = date.fromisoformat(args.today)
    return args


def main() -> int:
    args = _parse_args()
    report = build_report(args)
    args.output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {"total": report["total"], "counts_by_status": report["counts_by_status"]},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
