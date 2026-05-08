"""Refresh cached live candidate counts for the matching UI."""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any

from sqlalchemy import create_engine, delete, exists, select
from sqlalchemy.orm import Session

from app.api.matching import _live_candidate_count_for_product
from app.core.config import get_settings
from app.models import Product, ProductLiveCandidateCache
from app.models.competitor_item_match import CompetitorItemMatch, CompetitorItemMatchStatus

logger = logging.getLogger(__name__)

ACTIVE_MATCH_STATUSES = {
    CompetitorItemMatchStatus.ACCEPTED,
    CompetitorItemMatchStatus.SUGGESTED,
    CompetitorItemMatchStatus.NEEDS_REVIEW,
    CompetitorItemMatchStatus.AMBIGUOUS,
}


def _target_products_query(*, all_products: bool = False):
    query = select(Product).where(Product.is_active.is_(True))
    if all_products:
        return query
    return query.where(
        ~exists().where(
            CompetitorItemMatch.product_id == Product.id,
            CompetitorItemMatch.status.in_(ACTIVE_MATCH_STATUSES),
        )
    )


def refresh_live_candidate_cache(
    session: Session,
    *,
    product_ids: list[int] | None = None,
    all_products: bool = False,
    limit: int | None = None,
    max_seconds: int | None = None,
    batch_size: int = 500,
    progress_every: int = 1000,
    prune_stale: bool = False,
) -> dict[str, Any]:
    query = _target_products_query(all_products=all_products)
    if product_ids:
        query = query.where(Product.id.in_(product_ids))
    query = query.outerjoin(
        ProductLiveCandidateCache,
        ProductLiveCandidateCache.product_id == Product.id,
    ).order_by(
        ProductLiveCandidateCache.computed_at.isnot(None),
        ProductLiveCandidateCache.computed_at.asc(),
        Product.id.asc(),
    )
    if limit:
        query = query.limit(limit)

    products = session.execute(query).scalars().all()
    target_ids = [product.id for product in products]
    now = datetime.now(timezone.utc)
    started_at = monotonic()
    processed = 0
    with_live = 0
    stopped_by_time_limit = False

    if product_ids:
        session.execute(
            delete(ProductLiveCandidateCache).where(
                ProductLiveCandidateCache.product_id.in_(product_ids),
                ProductLiveCandidateCache.product_id.notin_(target_ids or [-1]),
            )
        )
    elif prune_stale:
        session.execute(
            delete(ProductLiveCandidateCache).where(
                ProductLiveCandidateCache.product_id.notin_(target_ids or [-1])
            )
        )

    cache_by_product = {
        row.product_id: row
        for row in session.execute(
            select(ProductLiveCandidateCache).where(
                ProductLiveCandidateCache.product_id.in_(target_ids or [-1])
            )
        )
        .scalars()
        .all()
    }

    for product in products:
        if max_seconds and monotonic() - started_at >= max_seconds:
            stopped_by_time_limit = True
            break
        live_count = _live_candidate_count_for_product(session, product)
        cache = cache_by_product.get(product.id)
        if cache is None:
            cache = ProductLiveCandidateCache(product_id=product.id)
            session.add(cache)
        cache.live_candidate_count = live_count
        cache.computed_at = now
        cache.updated_at = now
        processed += 1
        if live_count > 0:
            with_live += 1
        if processed % batch_size == 0:
            session.commit()
        if progress_every and processed % progress_every == 0:
            logger.info("refreshed %s live candidate cache rows", processed)

    session.commit()
    return {
        "processed": processed,
        "with_live_candidates": with_live,
        "without_live_candidates": processed - with_live,
        "all_products": all_products,
        "product_ids": product_ids or [],
        "limit": limit,
        "max_seconds": max_seconds,
        "stopped_by_time_limit": stopped_by_time_limit,
        "computed_at": now.isoformat(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh product live candidate count cache")
    parser.add_argument("--product-id", type=int, action="append", dest="product_ids")
    parser.add_argument("--all-products", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--max-seconds", type=int)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--progress-every", type=int, default=1000)
    parser.add_argument("--prune-stale", action="store_true")
    parser.add_argument("--report-file", type=Path)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    engine = create_engine(get_settings().database_url)
    with Session(engine) as session:
        report = refresh_live_candidate_cache(
            session,
            product_ids=args.product_ids,
            all_products=args.all_products,
            limit=args.limit,
            max_seconds=args.max_seconds,
            batch_size=args.batch_size,
            progress_every=args.progress_every,
            prune_stale=args.prune_stale,
        )

    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report_file:
        args.report_file.parent.mkdir(parents=True, exist_ok=True)
        args.report_file.write_text(output + "\n", encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
