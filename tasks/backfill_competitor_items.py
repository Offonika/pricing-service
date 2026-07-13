"""CLI для бэкапа каталога конкурентов из competitor_ftp_record в competitor_item."""

import argparse
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.infrastructure.db.engines import build_engine
from app.models import CompetitorFtpRecord, CompetitorItem, CompetitorItemSnapshot
from app.services.competitor_category import (
    CategoryClassifier,
    canonicalize_category,
    category_group,
)
from app.services.competitor_matching import _normalize_sku


def _norm_name(name: str | None) -> str | None:
    if not name:
        return None
    return " ".join(name.lower().split())


def backfill_items(
    session: Session,
    days_back: int | None = None,
    sources: Sequence[str] | None = None,
    limit: int | None = None,
    chunk_size: int = 1000,
    commit: bool = True,
) -> dict:
    query = select(CompetitorFtpRecord)
    if days_back:
        cutoff = datetime.now(UTC) - timedelta(days=days_back)
        query = query.where(CompetitorFtpRecord.observed_at >= cutoff)
    if sources:
        query = query.where(CompetitorFtpRecord.source.in_(list(sources)))
    if limit:
        query = query.limit(limit)

    stats = {"items_created": 0, "items_updated": 0, "snapshots_created": 0, "processed": 0}
    category_classifier: CategoryClassifier | None = None
    stream = session.execute(query).scalars().yield_per(chunk_size)
    for rec in stream:
        stats["processed"] += 1
        if not rec.sku:
            continue
        sku_norm = _normalize_sku(rec.sku)
        name_norm = _norm_name(rec.name)
        item = session.execute(
            select(CompetitorItem).where(
                CompetitorItem.competitor == rec.source,
                CompetitorItem.external_id == rec.sku,
            )
        ).scalar_one_or_none()
        is_new = item is None
        category_value = canonicalize_category(rec.group_name)
        if is_new and rec.name:
            if category_classifier is None:
                category_classifier = CategoryClassifier.from_env(force_llm=True)
            llm_category = category_classifier.classify(rec.name)
            if llm_category:
                category_value = canonicalize_category(llm_category) or category_value
        elif not category_value and rec.name and (is_new or not item.category):
            if category_classifier is None:
                category_classifier = CategoryClassifier.from_env()
            llm_category = category_classifier.classify(rec.name)
            if llm_category:
                category_value = canonicalize_category(llm_category)
        if is_new:
            item = CompetitorItem(
                competitor=rec.source,
                external_id=rec.sku,
                sku_norm=sku_norm,
                name=rec.name,
                name_norm=name_norm,
                category=category_value,
                category_group=category_group(category_value),
                price_opt=rec.price_opt,
                price_roz=rec.price_roz,
                availability=rec.in_stock,
                url=rec.link,
                scraped_at=rec.observed_at,
                first_seen_at=rec.file_date,
                last_seen_at=rec.file_date,
                parsed_device_brand=rec.parsed_device_brand,
                parsed_device_model=rec.parsed_device_model,
                parsed_device_variant=rec.parsed_device_variant,
                parse_confidence=rec.parse_confidence,
                parse_notes=rec.parse_notes,
            )
            session.add(item)
            stats["items_created"] += 1
        else:
            item.name = rec.name
            item.name_norm = name_norm
            if not item.category and category_value:
                item.category = category_value
            if item.category:
                item.category_group = category_group(item.category)
            item.price_opt = rec.price_opt
            item.price_roz = rec.price_roz
            item.availability = rec.in_stock
            item.url = rec.link
            item.scraped_at = rec.observed_at
            item.last_seen_at = rec.file_date
            if not item.sku_norm:
                item.sku_norm = sku_norm
            # не перетираем parsed_* если уже есть более точные данные
            if rec.parsed_device_brand and not item.parsed_device_brand:
                item.parsed_device_brand = rec.parsed_device_brand
            if rec.parsed_device_model and not item.parsed_device_model:
                item.parsed_device_model = rec.parsed_device_model
            if rec.parsed_device_variant and not item.parsed_device_variant:
                item.parsed_device_variant = rec.parsed_device_variant
            if rec.parse_confidence and not item.parse_confidence:
                item.parse_confidence = rec.parse_confidence
            if rec.parse_notes and not item.parse_notes:
                item.parse_notes = rec.parse_notes
            stats["items_updated"] += 1

        snapshot = CompetitorItemSnapshot(
            item=item,
            price_opt=rec.price_opt,
            price_roz=rec.price_roz,
            availability=rec.in_stock,
            scraped_at=rec.observed_at,
        )
        session.add(snapshot)
        stats["snapshots_created"] += 1

        if stats["processed"] % chunk_size == 0:
            session.flush()
            if commit:
                session.commit()
    if commit:
        session.commit()
    else:
        session.flush()
    if category_classifier is not None:
        category_classifier.close()
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill competitor_item from competitor_ftp_record."
    )
    parser.add_argument("--days-back", type=int, help="Only records observed within N days")
    parser.add_argument("--source", action="append", help="Filter by source (can repeat)")
    parser.add_argument("--limit", type=int, help="Limit records to process")
    parser.add_argument(
        "--chunk-size", type=int, default=1000, help="Flush/commit every N rows (default: 1000)"
    )
    parser.add_argument("--dry-run", action="store_true", help="Calculate changes and roll back")
    args = parser.parse_args()

    settings = get_settings()
    engine = build_engine(settings.database_url)
    with Session(engine) as session:
        stats = backfill_items(
            session,
            days_back=args.days_back,
            sources=args.source,
            limit=args.limit,
            chunk_size=args.chunk_size,
            commit=not args.dry_run,
        )
        if args.dry_run:
            session.rollback()
            stats["dry_run"] = True
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
