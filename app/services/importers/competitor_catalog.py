from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CompetitorItem, CompetitorItemSnapshot
from app.services.competitor_category import CategoryClassifier, category_group
from app.services.importers.zenlogs_moba import CompetitorCatalogRecord


@dataclass
class CatalogImportStats:
    items_created: int = 0
    items_updated: int = 0
    snapshots_created: int = 0
    errors: int = 0


def _next_snapshot_scraped_at(session: Session, item: CompetitorItem, scraped_at):
    candidate = scraped_at
    while session.scalar(
        select(CompetitorItemSnapshot.id).where(
            CompetitorItemSnapshot.competitor_item_id == item.id,
            CompetitorItemSnapshot.scraped_at == candidate,
        )
    ):
        candidate = candidate + timedelta(microseconds=1)
    return candidate


def upsert_catalog_records(
    session: Session, records: Iterable[CompetitorCatalogRecord]
) -> CatalogImportStats:
    stats = CatalogImportStats()
    category_classifier: CategoryClassifier | None = None
    try:
        for record in records:
            try:
                item = session.execute(
                    select(CompetitorItem).where(
                        CompetitorItem.competitor == record.competitor,
                        CompetitorItem.external_id == record.external_id,
                    )
                ).scalar_one_or_none()
                is_new = item is None
                category_value = record.category
                if not category_value and record.name and (is_new or not item.category):
                    if category_classifier is None:
                        category_classifier = CategoryClassifier.from_env()
                    category_value = category_classifier.classify(record.name)
                if is_new:
                    item = CompetitorItem(
                        competitor=record.competitor,
                        external_id=record.external_id,
                        name=record.name,
                        category=category_value,
                        category_group=category_group(category_value),
                        price_opt=record.price_opt,
                        price_roz=record.price_roz,
                        availability=record.availability,
                        url=record.url,
                        scraped_at=record.scraped_at,
                        first_seen_at=record.scraped_at,
                        last_seen_at=record.scraped_at,
                    )
                    session.add(item)
                    stats.items_created += 1
                else:
                    item.name = record.name
                    if record.category:
                        item.category = record.category
                    elif not item.category and category_value:
                        item.category = category_value
                    if item.category:
                        item.category_group = category_group(item.category)
                    item.price_opt = record.price_opt
                    item.price_roz = record.price_roz
                    item.availability = record.availability
                    item.url = record.url
                    item.scraped_at = record.scraped_at
                    item.last_seen_at = record.scraped_at
                    stats.items_updated += 1

                if item.id is None:
                    session.flush()
                snapshot = CompetitorItemSnapshot(
                    item=item,
                    price_opt=record.price_opt,
                    price_roz=record.price_roz,
                    availability=record.availability,
                    scraped_at=_next_snapshot_scraped_at(session, item, record.scraped_at),
                )
                session.add(snapshot)
                stats.snapshots_created += 1
            except Exception:
                session.rollback()
                stats.errors += 1
            else:
                session.flush()
    finally:
        if category_classifier is not None:
            category_classifier.close()
    return stats


__all__ = ["CatalogImportStats", "upsert_catalog_records"]
