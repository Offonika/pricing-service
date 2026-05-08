"""Backfill canonical phone model links from raw compatibilities."""

from __future__ import annotations

import argparse
import json
import logging
from datetime import date, datetime

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, selectinload

from app.core.config import get_settings
from app.models import CompetitorItem, CompetitorItemCompatibility, Product
from app.services.phone_model_canonicalization import PhoneModelCanonicalizer

logger = logging.getLogger("tasks.backfill_phone_model_links")


def _commit_batch(session: Session, counter: int, batch_size: int) -> None:
    if counter % batch_size == 0:
        session.commit()


def _merge_reason(notes: str | None, reason: str | None) -> str | None:
    parts: list[str] = []
    for part in (notes or "").split(";"):
        cleaned = part.strip()
        if cleaned and cleaned not in parts:
            parts.append(cleaned)
    cleaned_reason = (reason or "").strip()
    if cleaned_reason and cleaned_reason not in parts:
        parts.append(cleaned_reason)
    return "; ".join(parts) or None


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d").date()


def _log_progress(label: str, counter: int, progress_every: int) -> None:
    if progress_every > 0 and counter % progress_every == 0:
        logger.info("%s progress: processed=%s", label, counter)


def _apply_competitor_item_filters(
    query,
    *,
    first_seen_after: date | None,
    last_seen_after: date | None,
):
    if first_seen_after:
        query = query.where(func.date(CompetitorItem.first_seen_at) >= first_seen_after)
    if last_seen_after:
        query = query.where(func.date(CompetitorItem.last_seen_at) >= last_seen_after)
    return query


def run_backfill(
    session: Session,
    batch_size: int = 1000,
    *,
    process_products: bool = True,
    process_competitors: bool = True,
    competitor_first_seen_after: date | None = None,
    competitor_last_seen_after: date | None = None,
    product_limit: int | None = None,
    competitor_limit: int | None = None,
    progress_every: int = 5000,
) -> dict[str, int]:
    canonicalizer = PhoneModelCanonicalizer(session)
    stats = {
        "products_processed": 0,
        "product_links_created": 0,
        "product_unresolved": 0,
        "product_ambiguous": 0,
        "competitor_items_processed": 0,
        "competitor_links_created": 0,
        "competitor_unresolved": 0,
        "competitor_ambiguous": 0,
        "competitor_unlinked_noise": 0,
        "auto_created": 0,
    }

    if process_products:
        query = select(Product).options(selectinload(Product.compatibilities)).order_by(Product.id)
        if product_limit:
            query = query.limit(product_limit)
        products = session.execute(query).scalars().yield_per(batch_size)
        for product in products:
            stats["products_processed"] += 1
            raw_values = [
                compat.value for compat in product.compatibilities if compat.source == "onec"
            ]
            result = canonicalizer.sync_product_links(
                product=product, source="onec", raw_values=raw_values
            )
            stats["product_links_created"] += result["resolved"]
            stats["product_unresolved"] += result["unresolved"]
            stats["product_ambiguous"] += result["ambiguous"]
            stats["auto_created"] += result["auto_created"]
            _commit_batch(session, stats["products_processed"], batch_size)
            _log_progress(
                "product phone model backfill", stats["products_processed"], progress_every
            )

    if process_competitors:
        query = (
            select(CompetitorItemCompatibility)
            .join(
                CompetitorItem,
                CompetitorItem.id == CompetitorItemCompatibility.competitor_item_id,
            )
            .order_by(CompetitorItemCompatibility.id)
        )
        query = _apply_competitor_item_filters(
            query,
            first_seen_after=competitor_first_seen_after,
            last_seen_after=competitor_last_seen_after,
        )
        if competitor_limit:
            query = query.limit(competitor_limit)
        compats = session.execute(query).scalars().yield_per(batch_size)
        for comp in compats:
            stats["competitor_items_processed"] += 1
            result = canonicalizer.canonicalize(
                source="competitor_parser",
                raw_value=f"{comp.device_brand} {comp.device_model}",
                brand=comp.device_brand,
                model_name=comp.device_model,
                variant=comp.device_variant,
                confidence=1.0,
            )
            if result.phone_model is None:
                if result.ambiguous:
                    stats["competitor_ambiguous"] += 1
                else:
                    stats["competitor_unresolved"] += 1
                    stats["competitor_unlinked_noise"] += 1
                if comp.phone_model_id is not None:
                    comp.phone_model_id = None
                    session.add(comp)
                comp.notes = _merge_reason(comp.notes, result.reason)
                session.add(comp)
                _commit_batch(session, stats["competitor_items_processed"], batch_size)
                _log_progress(
                    "competitor compatibility backfill",
                    stats["competitor_items_processed"],
                    progress_every,
                )
                continue
            if comp.phone_model_id != result.phone_model.id:
                comp.phone_model_id = result.phone_model.id
                session.add(comp)
            if result.reason:
                comp.notes = result.reason
                session.add(comp)
            stats["competitor_links_created"] += 1
            if result.created_new:
                stats["auto_created"] += 1
            _commit_batch(session, stats["competitor_items_processed"], batch_size)
            _log_progress(
                "competitor compatibility backfill",
                stats["competitor_items_processed"],
                progress_every,
            )

        item_counter = 0
        item_query = select(CompetitorItem).where(
            CompetitorItem.parsed_device_brand.is_not(None),
            CompetitorItem.parsed_device_model.is_not(None),
        )
        item_query = _apply_competitor_item_filters(
            item_query,
            first_seen_after=competitor_first_seen_after,
            last_seen_after=competitor_last_seen_after,
        ).order_by(CompetitorItem.id)
        if competitor_limit:
            item_query = item_query.limit(competitor_limit)
        items = session.execute(item_query).scalars().yield_per(batch_size)
        for item in items:
            item_counter += 1
            result = canonicalizer.canonicalize(
                source="competitor_parser",
                raw_value=item.name,
                brand=item.parsed_device_brand,
                model_name=item.parsed_device_model,
                variant=item.parsed_device_variant,
                confidence=item.parse_confidence,
            )
            if result.created_new:
                stats["auto_created"] += 1
            _commit_batch(session, item_counter, batch_size)
            _log_progress("competitor item parsed backfill", item_counter, progress_every)

    session.commit()
    logger.info("phone model backfill completed", extra=stats)
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill canonical phone model links")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=1000,
        help="Commit transaction every N processed rows (default 1000)",
    )
    parser.add_argument("--products", action="store_true", help="Process product links")
    parser.add_argument("--competitors", action="store_true", help="Process competitor links")
    parser.add_argument(
        "--first-seen-after",
        help="Process competitor items with first_seen_at date >= YYYY-MM-DD",
    )
    parser.add_argument(
        "--last-seen-after",
        help="Process competitor items with last_seen_at date >= YYYY-MM-DD",
    )
    parser.add_argument("--product-limit", type=int, help="Limit products")
    parser.add_argument("--competitor-limit", type=int, help="Limit competitor rows")
    parser.add_argument(
        "--progress-every",
        type=int,
        default=5000,
        help="Log progress every N processed rows; 0 disables progress logs",
    )
    args = parser.parse_args()
    process_products = args.products or not args.competitors
    process_competitors = args.competitors or not args.products

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = get_settings()
    engine = create_engine(settings.database_url)
    with Session(engine) as session:
        result = run_backfill(
            session,
            batch_size=max(1, args.batch_size),
            process_products=process_products,
            process_competitors=process_competitors,
            competitor_first_seen_after=_parse_date(args.first_seen_after),
            competitor_last_seen_after=_parse_date(args.last_seen_after),
            product_limit=args.product_limit,
            competitor_limit=args.competitor_limit,
            progress_every=max(0, args.progress_every),
        )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
