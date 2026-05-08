"""Нормализация поля Вид_номенклатуры для product."""

from __future__ import annotations

import argparse
import logging

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Product
from app.services.nomenclature_kind import nomenclature_kind
from app.services.product_classification import recompute_product_classification


def normalize_kinds(
    session: Session,
    subject_in: list[str] | None,
    missing_only: bool,
    overwrite: bool,
    limit: int | None,
    chunk_size: int,
    min_id: int | None,
    max_id: int | None,
    active_only: bool,
    not_deleted_only: bool,
) -> dict:
    base_query = select(Product)
    if min_id is not None:
        base_query = base_query.where(Product.id >= min_id)
    if max_id is not None:
        base_query = base_query.where(Product.id <= max_id)
    if active_only:
        base_query = base_query.where(Product.is_active.is_(True))
    if not_deleted_only:
        base_query = base_query.where(Product.is_marked_for_deletion.is_(False))
    if subject_in:
        subject_norm = func.lower(func.trim(Product.subject))
        base_query = base_query.where(subject_norm.in_(subject_in))
    if missing_only:
        base_query = base_query.where(Product.vid_nomenklatury_generated.is_(None))

    processed = 0
    updated = 0
    total_seen = 0
    last_id: int | None = None
    while True:
        query = base_query
        if last_id is not None:
            query = query.where(Product.id > last_id)
        query = query.order_by(Product.id).limit(chunk_size)
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
        for product in batch:
            processed += 1
            subject = product.subject_generated or product.subject_1c or product.subject
            kind = nomenclature_kind(subject, product.name)
            if not kind:
                continue
            if product.vid_nomenklatury_generated and not overwrite:
                continue
            product.vid_nomenklatury_generated = kind
            recompute_product_classification(product)
            session.add(product)
            updated += 1
            logging.info("generated vid_nomenklatury set %s -> %s", product.article, kind)
        session.commit()

    return {
        "processed": processed,
        "updated": updated,
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Normalize product.Вид_номенклатуры.")
    parser.add_argument("--subject-in", help="Comma-separated subject filter (case-insensitive)")
    parser.add_argument(
        "--missing-only", action="store_true", help="Only items without Вид_номенклатуры"
    )
    parser.add_argument(
        "--overwrite", action="store_true", help="Overwrite existing Вид_номенклатуры"
    )
    parser.add_argument("--limit", type=int, help="Limit records")
    parser.add_argument("--active-only", action="store_true", help="Only active products")
    parser.add_argument(
        "--not-deleted-only", action="store_true", help="Exclude products marked for deletion"
    )
    parser.add_argument(
        "--chunk-size", type=int, default=500, help="Commit every N records (default: 500)"
    )
    parser.add_argument("--min-id", type=int, help="Only process items with id >= min-id")
    parser.add_argument("--max-id", type=int, help="Only process items with id <= max-id")
    args = parser.parse_args()

    subject_in = None
    if args.subject_in:
        subject_in = [
            " ".join(part.strip().lower().split())
            for part in args.subject_in.split(",")
            if part.strip()
        ]

    settings = get_settings()
    engine = create_engine(settings.database_url)
    with Session(engine) as session:
        stats = normalize_kinds(
            session,
            subject_in=subject_in,
            missing_only=args.missing_only,
            overwrite=args.overwrite,
            limit=args.limit,
            chunk_size=max(args.chunk_size, 1),
            min_id=args.min_id,
            max_id=args.max_id,
            active_only=args.active_only,
            not_deleted_only=args.not_deleted_only,
        )
        print(stats)


if __name__ == "__main__":
    main()
