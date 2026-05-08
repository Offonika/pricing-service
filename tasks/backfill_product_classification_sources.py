"""Backfill legacy product classification fields into split source columns."""

from __future__ import annotations

import argparse
import json

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Product
from app.services.product_classification import (
    CLASSIFICATION_SOURCE_1C,
    CLASSIFICATION_SOURCE_GENERATED,
    recompute_product_classification,
)


def _backfill_subject(product: Product) -> bool:
    changed = False
    if product.subject is None:
        return changed

    if product.subject_1c is None and product.subject_source == CLASSIFICATION_SOURCE_1C:
        product.subject_1c = product.subject
        changed = True
    elif (
        product.subject_generated is None
        and product.subject_source == CLASSIFICATION_SOURCE_GENERATED
    ):
        product.subject_generated = product.subject
        changed = True
    elif product.subject_1c is None and product.subject_generated is None:
        product.subject_generated = product.subject
        changed = True

    return changed


def _backfill_kind(product: Product) -> bool:
    changed = False
    if product.vid_nomenklatury is None:
        return changed

    if (
        product.vid_nomenklatury_1c is None
        and product.vid_nomenklatury_source == CLASSIFICATION_SOURCE_1C
    ):
        product.vid_nomenklatury_1c = product.vid_nomenklatury
        changed = True
    elif (
        product.vid_nomenklatury_generated is None
        and product.vid_nomenklatury_source == CLASSIFICATION_SOURCE_GENERATED
    ):
        product.vid_nomenklatury_generated = product.vid_nomenklatury
        changed = True
    elif product.vid_nomenklatury_1c is None and product.vid_nomenklatury_generated is None:
        product.vid_nomenklatury_generated = product.vid_nomenklatury
        changed = True

    return changed


def backfill_product_classification_sources(
    session: Session, limit: int | None = None, chunk_size: int = 500
) -> dict[str, int]:
    updated = 0
    processed = 0
    last_id: int | None = None

    while True:
        query = select(Product).order_by(Product.id).limit(chunk_size)
        if last_id is not None:
            query = query.where(Product.id > last_id)
        if limit is not None:
            remaining = limit - processed
            if remaining <= 0:
                break
            if remaining < chunk_size:
                query = query.limit(remaining)

        products = list(session.execute(query).scalars())
        if not products:
            break

        last_id = products[-1].id
        for product in products:
            processed += 1
            changed = False
            changed = _backfill_subject(product) or changed
            changed = _backfill_kind(product) or changed
            if changed:
                recompute_product_classification(product)
                session.add(product)
                updated += 1
        session.commit()

    return {"processed": processed, "updated": updated}


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill split product classification columns.")
    parser.add_argument("--limit", type=int, help="Limit records to process")
    parser.add_argument(
        "--chunk-size", type=int, default=500, help="Commit every N records (default: 500)"
    )
    args = parser.parse_args()

    settings = get_settings()
    engine = create_engine(settings.database_url)
    with Session(engine) as session:
        stats = backfill_product_classification_sources(
            session=session,
            limit=args.limit,
            chunk_size=max(args.chunk_size, 1),
        )
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
