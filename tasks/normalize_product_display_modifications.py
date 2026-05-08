"""Нормализация derived-свойств модификации дисплеев у наших товаров."""

from __future__ import annotations

import argparse
import csv
import json
import logging
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Product
from app.services.product_display_modification import (
    DISPLAY_MODIFICATION_PARSE_VERSION,
    STATUS_CONFLICT,
    analyze_product_display_modification,
    apply_product_display_modification,
    is_display_product,
)

logger = logging.getLogger(__name__)

REPORT_FIELDS = [
    "product_id",
    "article",
    "name",
    "in_frame",
    "onec_has_frame",
    "parsed_has_frame",
    "display_has_frame",
    "status",
    "source",
    "confidence",
    "parsed_screen_kit",
    "parsed_has_touch",
    "parsed_has_ic_pad",
    "parsed_has_binding_no_solder",
    "parsed_backlight",
    "parsed_matrix_tags",
    "notes",
    "parse_version",
]


def _write_csv(path: str, rows: list[dict[str, object]]) -> None:
    report_path = Path(path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REPORT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def _write_xlsx(path: str, rows: list[dict[str, object]]) -> None:
    from openpyxl import Workbook

    report_path = Path(path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "display_modifications"
    sheet.append(REPORT_FIELDS)
    for row in rows:
        sheet.append([row.get(field) for field in REPORT_FIELDS])
    workbook.save(report_path)


def _write_report(path: str, rows: list[dict[str, object]]) -> None:
    if Path(path).suffix.lower() == ".xlsx":
        _write_xlsx(path, rows)
    else:
        _write_csv(path, rows)


def normalize_products(
    session: Session,
    *,
    apply: bool,
    report: str | None,
    conflicts_report: str | None,
    limit: int | None,
    article: str | None,
    only_conflicts: bool,
    parse_version: str,
) -> dict[str, int]:
    query = select(Product).where(Product.is_active.is_(True)).order_by(Product.id)
    if article:
        query = query.where(Product.article == article)
    if limit:
        query = query.limit(limit)

    stats = {
        "scanned": 0,
        "display_products": 0,
        "updated": 0,
        "confirmed": 0,
        "derived_from_name": 0,
        "derived_from_onec": 0,
        "conflict": 0,
        "unknown": 0,
    }
    report_rows: list[dict[str, object]] = []
    conflict_rows: list[dict[str, object]] = []

    for product in session.execute(query).scalars():
        stats["scanned"] += 1
        if not is_display_product(product):
            continue
        stats["display_products"] += 1

        result = analyze_product_display_modification(product, parse_version=parse_version)
        stats[result.status] += 1
        if only_conflicts and result.status != STATUS_CONFLICT:
            continue

        row = result.as_report_row()
        report_rows.append(row)
        if result.status == STATUS_CONFLICT:
            conflict_rows.append(row)

        if apply:
            apply_product_display_modification(product, result)
            session.add(product)
            stats["updated"] += 1

    if apply:
        session.commit()

    if report:
        _write_report(report, report_rows)
    if conflicts_report:
        _write_report(conflicts_report, conflict_rows)

    return stats


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Записать derived-поля в product")
    parser.add_argument("--dry-run", action="store_true", help="Не записывать изменения")
    parser.add_argument("--report", help="CSV/XLSX-отчет по всем обработанным дисплеям")
    parser.add_argument("--conflicts-report", help="CSV/XLSX-отчет только по конфликтам")
    parser.add_argument("--limit", type=int, help="Ограничить количество товаров")
    parser.add_argument("--article", help="Обработать один артикул")
    parser.add_argument(
        "--only-conflicts", action="store_true", help="В отчет включать только конфликты"
    )
    parser.add_argument(
        "--parse-version",
        default=DISPLAY_MODIFICATION_PARSE_VERSION,
        help="Версия правил парсинга",
    )
    args = parser.parse_args()

    if args.dry_run and args.apply:
        parser.error("--dry-run и --apply нельзя использовать вместе")

    settings = get_settings()
    engine = create_engine(settings.database_url)
    with Session(engine) as session:
        stats = normalize_products(
            session,
            apply=args.apply,
            report=args.report,
            conflicts_report=args.conflicts_report,
            limit=args.limit,
            article=args.article,
            only_conflicts=args.only_conflicts,
            parse_version=args.parse_version,
        )
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    logger.info("normalize_product_display_modifications done: %s", stats)


if __name__ == "__main__":
    main()
