"""Refresh URL aliases for competitor_item catalog rows."""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime

import httpx
from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.infrastructure.db.engines import build_engine
from app.models import CompetitorItem, CompetitorItemUrlAlias
from app.services.competitor_url_aliases import (
    parse_competitor_url,
    upsert_competitor_item_url_alias,
    upsert_resolved_poiskzip_alias,
)

logger = logging.getLogger(__name__)


def refresh_competitor_url_aliases(
    session: Session,
    *,
    source: str | None = None,
    external_ids: list[str] | None = None,
    first_seen_after: datetime | None = None,
    last_seen_after: datetime | None = None,
    limit: int | None = None,
    resolve_poiskzip: bool = False,
    only_missing_direct: bool = False,
    timeout: float = 8.0,
    batch_size: int = 500,
) -> dict[str, int]:
    query = select(CompetitorItem).where(
        CompetitorItem.is_active.is_(True),
        CompetitorItem.url.isnot(None),
    )
    if source:
        query = query.where(CompetitorItem.competitor == source)
    if external_ids:
        query = query.where(CompetitorItem.external_id.in_(external_ids))
    if first_seen_after:
        query = query.where(func.date(CompetitorItem.first_seen_at) >= first_seen_after.date())
    if last_seen_after:
        query = query.where(func.date(CompetitorItem.last_seen_at) >= last_seen_after.date())
    if only_missing_direct:
        query = query.where(
            ~exists().where(
                CompetitorItemUrlAlias.competitor_item_id == CompetitorItem.id,
                CompetitorItemUrlAlias.catalog_id.isnot(None),
            )
        )
    query = query.order_by(CompetitorItem.id)
    if limit:
        query = query.limit(limit)

    processed = 0
    stored_aliases = 0
    resolved_aliases = 0
    resolve_failed = 0
    skipped_non_redirect = 0

    http_client = (
        httpx.Client(timeout=timeout, follow_redirects=False) if resolve_poiskzip else None
    )
    try:
        for item in session.execute(query).scalars():
            processed += 1
            stored = upsert_competitor_item_url_alias(
                session,
                item,
                item.url,
                url_kind="stored",
            )
            if stored is not None:
                stored_aliases += 1

            parts = parse_competitor_url(item.url)
            if resolve_poiskzip and parts and parts.redirect_id:
                try:
                    resolved = upsert_resolved_poiskzip_alias(
                        session,
                        item,
                        client=http_client,
                        timeout=timeout,
                    )
                    if resolved is not None:
                        resolved_aliases += 1
                    else:
                        resolve_failed += 1
                except Exception:  # noqa: BLE001
                    resolve_failed += 1
                    logger.exception(
                        "failed to resolve competitor redirect",
                        extra={"competitor": item.competitor, "external_id": item.external_id},
                    )
            elif resolve_poiskzip:
                skipped_non_redirect += 1

            if batch_size > 0 and processed % batch_size == 0:
                session.commit()
        session.commit()
    finally:
        if http_client is not None:
            http_client.close()

    return {
        "processed": processed,
        "stored_aliases": stored_aliases,
        "resolved_aliases": resolved_aliases,
        "resolve_failed": resolve_failed,
        "skipped_non_redirect": skipped_non_redirect,
    }


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(description="Refresh competitor item URL aliases")
    parser.add_argument("--source", help="Filter by competitor")
    parser.add_argument("--external-id", action="append", dest="external_ids")
    parser.add_argument("--first-seen-after", help="YYYY-MM-DD")
    parser.add_argument("--last-seen-after", help="YYYY-MM-DD")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--resolve-poiskzip", action="store_true")
    parser.add_argument("--only-missing-direct", action="store_true")
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--batch-size", type=int, default=500)
    args = parser.parse_args()

    engine = build_engine(get_settings().database_url)
    with Session(engine) as session:
        stats = refresh_competitor_url_aliases(
            session,
            source=args.source,
            external_ids=args.external_ids,
            first_seen_after=_parse_date(args.first_seen_after),
            last_seen_after=_parse_date(args.last_seen_after),
            limit=args.limit,
            resolve_poiskzip=args.resolve_poiskzip,
            only_missing_direct=args.only_missing_direct,
            timeout=args.timeout,
            batch_size=args.batch_size,
        )
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
