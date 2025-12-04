from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CompetitorItem, CompetitorItemSnapshot
from app.services.importers.zenlogs_moba import CompetitorCatalogRecord


@dataclass
class CatalogImportStats:
    items_created: int = 0
    items_updated: int = 0
    snapshots_created: int = 0
    errors: int = 0


def upsert_catalog_records(session: Session, records: Iterable[CompetitorCatalogRecord]) -> CatalogImportStats:
    stats = CatalogImportStats()
    for record in records:
        try:
            item = session.execute(
                select(CompetitorItem).where(
                    CompetitorItem.competitor == record.competitor,
                    CompetitorItem.external_id == record.external_id,
                )
            ).scalar_one_or_none()
            is_new = item is None
            if is_new:
                item = CompetitorItem(
                    competitor=record.competitor,
                    external_id=record.external_id,
                    name=record.name,
                    category=record.category,
                    price_opt=record.price_opt,
                    price_roz=record.price_roz,
                    availability=record.availability,
                    url=record.url,
                    scraped_at=record.scraped_at,
                )
                session.add(item)
                stats.items_created += 1
            else:
                item.name = record.name
                item.category = record.category
                item.price_opt = record.price_opt
                item.price_roz = record.price_roz
                item.availability = record.availability
                item.url = record.url
                item.scraped_at = record.scraped_at
                stats.items_updated += 1

            snapshot = CompetitorItemSnapshot(
                item=item,
                price_opt=record.price_opt,
                price_roz=record.price_roz,
                availability=record.availability,
                scraped_at=record.scraped_at,
            )
            session.add(snapshot)
            stats.snapshots_created += 1
        except Exception:
            session.rollback()
            stats.errors += 1
        else:
            session.flush()
    return stats


__all__ = ["CatalogImportStats", "upsert_catalog_records"]
