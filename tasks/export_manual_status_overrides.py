"""Выгрузка согласованных ручных статусов в файл ручных решений автозаказа.

Запускать перед `tasks/build_assortment_lifecycle_facts.py`: без этого шага
решение менеджера («Допродаём», «Не закупать» и другие ручные статусы) остаётся
внутри приложения «Формирование заказа» и не влияет на ночной расчёт.
"""

from __future__ import annotations

import argparse
import json

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.infrastructure.db.engines import build_engine
from app.services.procurement_manual_status_export import (
    blocked_rows,
    export_manual_status_overrides,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export approved manual assortment statuses into the auto-order overrides file."
    )
    parser.add_argument(
        "--overrides-path",
        help="Path to display-manual-overrides.json; defaults to the configured one.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only report what would change, without writing the file.",
    )
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = get_settings()
    overrides_path = args.overrides_path or settings.procurement_assortment_manual_overrides_path
    engine = build_engine(settings.database_url)
    with Session(engine) as db:
        decisions, merge_rows = export_manual_status_overrides(
            db,
            overrides_path,
            dry_run=args.dry_run,
        )
    blocked = blocked_rows(merge_rows)
    payload = {
        "overrides_path": overrides_path,
        "dry_run": args.dry_run,
        "decisions": len(decisions),
        "added": sum(1 for row in merge_rows if row.get("action") == "added"),
        "updated": sum(1 for row in merge_rows if row.get("action") == "updated"),
        "blocked": len(blocked),
        "blocked_codes": [row.get("nomenclature_code") for row in blocked],
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(
            f"manual status overrides: {payload['decisions']} decisions, "
            f"+{payload['added']} added, ~{payload['updated']} updated, "
            f"{payload['blocked']} blocked"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
