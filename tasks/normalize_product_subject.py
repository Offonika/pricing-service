"""Нормализация поля subject (Предмет) для нашей номенклатуры (product)."""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Iterable

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Product
from app.services.competitor_category import CategoryClassifier
from app.services.nomenclature_kind import nomenclature_kind
from app.services.product_classification import recompute_product_classification

DEFAULT_UNKNOWN_VALUES = (
    "unknown",
    "undefined",
    "неизвестно",
    "не определено",
    "н/д",
    "нет",
    "-",
)


def _normalize_unknown_values(values: Iterable[str] | None) -> tuple[str, ...]:
    if values is None:
        return DEFAULT_UNKNOWN_VALUES
    normalized = []
    for value in values:
        if value is None:
            continue
        cleaned = " ".join(str(value).strip().lower().split())
        if cleaned:
            normalized.append(cleaned)
    return tuple(normalized) or DEFAULT_UNKNOWN_VALUES


def normalize_subjects(
    session: Session,
    name_contains: str | None,
    name_not_startswith: str | None,
    subject_in: list[str] | None,
    missing_only: bool,
    overwrite: bool,
    limit: int | None,
    use_llm: bool,
    llm_limit: int,
    llm_only: bool,
    force_llm: bool,
    default_category: str | None,
    treat_unknown_as_missing: bool,
    unknown_values: Iterable[str] | None,
    chunk_size: int,
    min_id: int | None,
    max_id: int | None,
    active_only: bool,
    not_deleted_only: bool,
) -> dict:
    unknown_values_norm = (
        _normalize_unknown_values(unknown_values) if treat_unknown_as_missing else ()
    )
    base_query = select(Product)
    if name_contains:
        base_query = base_query.where(Product.name.ilike(f"%{name_contains}%"))
    if name_not_startswith:
        prefix = name_not_startswith.strip()
        if prefix:
            base_query = base_query.where(~func.ltrim(Product.name).ilike(f"{prefix}%"))
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
        if treat_unknown_as_missing:
            subject_norm = func.lower(func.trim(Product.subject_generated))
            base_query = base_query.where(
                (Product.subject_generated.is_(None)) | (subject_norm.in_(unknown_values_norm))
            )
        else:
            base_query = base_query.where(Product.subject_generated.is_(None))

    classifier = CategoryClassifier.from_env(
        use_llm=use_llm,
        llm_limit=llm_limit,
        llm_only=llm_only,
        force_llm=force_llm,
        default_category=default_category,
    )

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
            if not product.name:
                continue
            processed += 1
            existing = None
            if product.subject_generated:
                existing = " ".join(product.subject_generated.strip().lower().split())
            if treat_unknown_as_missing and existing in unknown_values_norm:
                existing = None
            subject = classifier.classify(product.name)
            if not subject:
                continue
            if existing and not overwrite:
                continue
            product.subject_generated = subject
            product.vid_nomenklatury_generated = nomenclature_kind(subject, product.name)
            recompute_product_classification(product)
            session.add(product)
            updated += 1
            logging.info("generated subject set %s -> %s", product.article, subject)
        session.commit()
    classifier.close()
    return {
        "processed": processed,
        "updated": updated,
        "llm_used": classifier.llm_calls,
        "llm_failed": classifier.llm_failed,
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Normalize product.subject (Предмет).")
    parser.add_argument("--name-contains", help="ILIKE on name")
    parser.add_argument(
        "--name-not-startswith", help="Exclude names starting with prefix (case-insensitive)"
    )
    parser.add_argument("--subject-in", help="Comma-separated subject filter (case-insensitive)")
    parser.add_argument("--missing-only", action="store_true", help="Only items without subject")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing subject")
    parser.add_argument("--limit", type=int, help="Limit records")
    parser.add_argument("--llm", action="store_true", help="Use LLM fallback")
    parser.add_argument("--llm-limit", type=int, default=0, help="Max LLM calls (0 = no limit)")
    parser.add_argument(
        "--llm-only", action="store_true", help="Skip rule-based classification, use only LLM"
    )
    parser.add_argument(
        "--force-llm", action="store_true", help="Run LLM even if rules matched (fallback to rules)"
    )
    parser.add_argument(
        "--default-category", help="Set fallback category when no match (default: env)"
    )
    parser.add_argument(
        "--treat-unknown", action="store_true", help="Treat 'unknown' values as missing"
    )
    parser.add_argument(
        "--unknown-values",
        help="Comma-separated list of subject values treated as missing (overrides defaults)",
    )
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

    unknown_values = None
    subject_in = None
    if args.unknown_values:
        unknown_values = [part.strip() for part in args.unknown_values.split(",") if part.strip()]
    if args.subject_in:
        subject_in = [
            " ".join(part.strip().lower().split())
            for part in args.subject_in.split(",")
            if part.strip()
        ]

    settings = get_settings()
    engine = create_engine(settings.database_url)
    with Session(engine) as session:
        stats = normalize_subjects(
            session,
            name_contains=args.name_contains,
            name_not_startswith=args.name_not_startswith,
            subject_in=subject_in,
            missing_only=args.missing_only,
            overwrite=args.overwrite,
            limit=args.limit,
            use_llm=args.llm,
            llm_limit=args.llm_limit,
            llm_only=args.llm_only,
            force_llm=args.force_llm,
            default_category=args.default_category,
            treat_unknown_as_missing=args.treat_unknown,
            unknown_values=unknown_values,
            chunk_size=max(args.chunk_size, 1),
            min_id=args.min_id,
            max_id=args.max_id,
            active_only=args.active_only,
            not_deleted_only=args.not_deleted_only,
        )
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
