"""Нормализация значений competitor_item.category (регистр/синонимы)."""

from __future__ import annotations

import argparse
import json
import logging

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import CompetitorItem
from app.services.competitor_category import canonicalize_category, category_group


def canonicalize_categories(
    session: Session,
    source: str | None,
    limit: int | None,
    chunk_size: int,
) -> dict:
    base_query = select(CompetitorItem).where(CompetitorItem.category.is_not(None))
    if source:
        base_query = base_query.where(CompetitorItem.competitor == source)

    processed = 0
    updated = 0
    last_id: int | None = None
    total_seen = 0

    while True:
        query = base_query
        if last_id is not None:
            query = query.where(CompetitorItem.id > last_id)
        query = query.order_by(CompetitorItem.id).limit(chunk_size)
        if limit:
            remaining = limit - total_seen
            if remaining <= 0:
                break
            if remaining < chunk_size:
                query = query.limit(remaining)
        batch = list(session.execute(query).scalars())
        if not batch:
            break
        total_seen += len(batch)
        last_id = batch[-1].id
        for item in batch:
            processed += 1
            current = item.category
            normalized = canonicalize_category(current)
            group_value = category_group(normalized or current)
            changed = False
            if normalized and normalized != current:
                item.category = normalized
                changed = True
            if group_value and item.category_group != group_value:
                item.category_group = group_value
                changed = True
            if changed:
                session.add(item)
                updated += 1
        session.commit()

    return {"processed": processed, "updated": updated}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Canonicalize competitor_item.category values.")
    parser.add_argument("--source", help="Filter by competitor")
    parser.add_argument("--limit", type=int, help="Limit records")
    parser.add_argument(
        "--chunk-size", type=int, default=1000, help="Commit every N records (default: 1000)"
    )
    args = parser.parse_args()

    settings = get_settings()
    engine = create_engine(settings.database_url)
    with Session(engine) as session:
        stats = canonicalize_categories(
            session,
            source=args.source,
            limit=args.limit,
            chunk_size=max(args.chunk_size, 1),
        )
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
