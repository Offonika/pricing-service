"""Нормализация поля category (категория) в каталоге конкурентов."""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Iterable

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import CompetitorItem
from app.services.competitor_category import CategoryClassifier, category_group
from tasks.canonicalize_competitor_categories import canonicalize_categories

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


def normalize_categories(
    session: Session,
    source: str | None,
    name_contains: str | None,
    name_not_startswith: str | None,
    category_in: list[str] | None,
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
) -> dict:
    unknown_values_norm = (
        _normalize_unknown_values(unknown_values) if treat_unknown_as_missing else ()
    )
    base_query = select(CompetitorItem)
    if source:
        base_query = base_query.where(CompetitorItem.competitor == source)
    if name_contains:
        base_query = base_query.where(CompetitorItem.name.ilike(f"%{name_contains}%"))
    if name_not_startswith:
        prefix = name_not_startswith.strip()
        if prefix:
            base_query = base_query.where(~func.ltrim(CompetitorItem.name).ilike(f"{prefix}%"))
    if min_id is not None:
        base_query = base_query.where(CompetitorItem.id >= min_id)
    if max_id is not None:
        base_query = base_query.where(CompetitorItem.id <= max_id)
    if category_in:
        category_norm = func.lower(func.trim(CompetitorItem.category))
        base_query = base_query.where(category_norm.in_(category_in))
    if missing_only:
        if treat_unknown_as_missing:
            category_norm = func.lower(func.trim(CompetitorItem.category))
            base_query = base_query.where(
                (CompetitorItem.category.is_(None)) | (category_norm.in_(unknown_values_norm))
            )
        else:
            base_query = base_query.where(CompetitorItem.category.is_(None))

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
            if not item.name:
                continue
            processed += 1
            existing = None
            if item.category:
                existing = " ".join(item.category.strip().lower().split())
            if treat_unknown_as_missing and existing in unknown_values_norm:
                existing = None
            category = classifier.classify(item.name)
            if not category:
                continue
            if existing and not overwrite:
                continue
            item.category = category
            item.category_group = category_group(category)
            session.add(item)
            updated += 1
            logging.info(
                "category set %s/%s -> %s",
                item.competitor,
                item.external_id,
                category,
            )
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
    parser = argparse.ArgumentParser(
        description="Normalize competitor_item.category (ru category)."
    )
    parser.add_argument("--source", help="Filter by competitor")
    parser.add_argument("--name-contains", help="ILIKE on name")
    parser.add_argument(
        "--name-not-startswith", help="Exclude names starting with prefix (case-insensitive)"
    )
    parser.add_argument("--category-in", help="Comma-separated category filter (case-insensitive)")
    parser.add_argument("--missing-only", action="store_true", help="Only items without category")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing category")
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
        help="Comma-separated list of category values treated as missing (overrides defaults)",
    )
    parser.add_argument(
        "--chunk-size", type=int, default=500, help="Commit every N records (default: 500)"
    )
    parser.add_argument("--min-id", type=int, help="Only process items with id >= min-id")
    parser.add_argument("--max-id", type=int, help="Only process items with id <= max-id")
    parser.add_argument(
        "--skip-canonicalize",
        action="store_true",
        help="Skip canonicalization pass after classification",
    )
    args = parser.parse_args()
    unknown_values = None
    category_in = None
    if args.unknown_values:
        unknown_values = [part.strip() for part in args.unknown_values.split(",") if part.strip()]
    if args.category_in:
        category_in = [
            " ".join(part.strip().lower().split())
            for part in args.category_in.split(",")
            if part.strip()
        ]

    settings = get_settings()
    engine = create_engine(settings.database_url)
    with Session(engine) as session:
        stats = normalize_categories(
            session,
            source=args.source,
            name_contains=args.name_contains,
            name_not_startswith=args.name_not_startswith,
            category_in=category_in,
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
        )
        canonicalize_stats = None
        if not args.skip_canonicalize:
            canonicalize_stats = canonicalize_categories(
                session,
                source=args.source,
                limit=None,
                chunk_size=max(args.chunk_size, 1),
            )
            stats["canonicalize_processed"] = canonicalize_stats.get("processed", 0)
            stats["canonicalize_updated"] = canonicalize_stats.get("updated", 0)
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
