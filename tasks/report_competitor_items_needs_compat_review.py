"""Отчет по товарам конкурентов, где нужна ручная/LLM разметка совместимости."""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.infrastructure.db.engines import build_engine
from app.models import CompetitorItem, CompetitorItemCompatibility
from app.services.matching_guardrails import competitor_item_requires_compatibility


def _parse_date(value: str | None):
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d").date()


def build_review_items(
    session: Session,
    *,
    first_seen_after: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    first_seen_date = _parse_date(first_seen_after)
    query = (
        select(CompetitorItem)
        .where(~exists().where(CompetitorItemCompatibility.competitor_item_id == CompetitorItem.id))
        .order_by(CompetitorItem.item_type, CompetitorItem.competitor, CompetitorItem.external_id)
    )
    if first_seen_date:
        query = query.where(func.date(CompetitorItem.first_seen_at) >= first_seen_date)

    rows: list[dict[str, Any]] = []
    for item in session.execute(query).scalars():
        target = competitor_item_requires_compatibility(item)
        if not target.requires_compatibility:
            continue
        rows.append(
            {
                "competitor_item_id": item.id,
                "competitor": item.competitor,
                "external_id": item.external_id,
                "name": item.name,
                "item_type": item.item_type,
                "category": item.category,
                "category_group": item.category_group,
                "parsed_device_brand": item.parsed_device_brand,
                "parsed_device_model": item.parsed_device_model,
                "parsed_device_variant": item.parsed_device_variant,
                "parse_notes": item.parse_notes,
                "first_seen_at": item.first_seen_at.isoformat() if item.first_seen_at else None,
                "last_seen_at": item.last_seen_at.isoformat() if item.last_seen_at else None,
                "review_reason": target.reason,
            }
        )
        if limit and len(rows) >= limit:
            break
    return rows


def _write_json(path: Path, items: list[dict[str, Any]]) -> None:
    payload = {"total": len(items), "items": items}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_csv(path: Path, items: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "competitor_item_id",
        "competitor",
        "external_id",
        "name",
        "item_type",
        "category",
        "category_group",
        "parsed_device_brand",
        "parsed_device_model",
        "parsed_device_variant",
        "parse_notes",
        "first_seen_at",
        "last_seen_at",
        "review_reason",
    ]
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(items)


def main() -> None:
    parser = argparse.ArgumentParser(description="Report competitor items needing compat review")
    parser.add_argument("--first-seen-after", help="Filter by first_seen_at date >= YYYY-MM-DD")
    parser.add_argument("--limit", type=int, help="Limit report rows")
    parser.add_argument("--report-file", help="Write JSON report to file")
    parser.add_argument("--report-csv", help="Write CSV report to file")
    args = parser.parse_args()

    settings = get_settings()
    engine = build_engine(settings.database_url)
    with Session(engine) as session:
        items = build_review_items(
            session,
            first_seen_after=args.first_seen_after,
            limit=args.limit,
        )

    payload = {"total": len(items), "items": items}
    if args.report_file:
        _write_json(Path(args.report_file), items)
    if args.report_csv:
        _write_csv(Path(args.report_csv), items)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
