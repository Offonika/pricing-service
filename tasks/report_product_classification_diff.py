"""Отчёт по расхождениям между классификацией из 1С и сгенерированной."""

from __future__ import annotations

import argparse
import json

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.infrastructure.db.engines import build_engine
from app.models import Product


def build_report(session: Session, limit: int | None = None) -> dict:
    query = (
        select(Product)
        .where(
            or_(
                Product.subject_1c.is_distinct_from(Product.subject_generated),
                Product.vid_nomenklatury_1c.is_distinct_from(Product.vid_nomenklatury_generated),
            )
        )
        .order_by(Product.id)
    )
    if limit:
        query = query.limit(limit)

    products = list(session.execute(query).scalars())
    items = [
        {
            "article": product.article,
            "name": product.name,
            "subject_1c": product.subject_1c,
            "subject_generated": product.subject_generated,
            "subject_final": product.subject,
            "subject_source": product.subject_source,
            "vid_nomenklatury_1c": product.vid_nomenklatury_1c,
            "vid_nomenklatury_generated": product.vid_nomenklatury_generated,
            "vid_nomenklatury_final": product.vid_nomenklatury,
            "vid_nomenklatury_source": product.vid_nomenklatury_source,
        }
        for product in products
    ]
    return {"count": len(items), "items": items}


def main() -> None:
    parser = argparse.ArgumentParser(description="Report product classification diffs.")
    parser.add_argument("--limit", type=int, help="Limit rows in the report")
    args = parser.parse_args()

    settings = get_settings()
    engine = build_engine(settings.database_url)
    with Session(engine) as session:
        report = build_report(session, limit=args.limit)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
