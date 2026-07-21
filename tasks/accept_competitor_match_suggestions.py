"""Bulk-accept generated competitor match suggestions."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.core.config import get_settings
from app.infrastructure.db.engines import build_engine
from app.models import CompetitorItem
from app.models.competitor_item_match import (
    CompetitorItemMatch,
    CompetitorItemMatchMethod,
    CompetitorItemMatchStatus,
)

UTC = timezone.utc


def _float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_date(value: str | None):
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d").date()


def _match_score(match: CompetitorItemMatch) -> float | None:
    score = _float(match.final_score or match.score_llm or match.score_embed_best)
    if score is not None:
        return score
    rationale = match.rationale_json or {}
    return _float(rationale.get("best_score"))


def _sample_row(match: CompetitorItemMatch) -> dict[str, Any]:
    item = match.competitor_item
    product = match.product
    return {
        "match_id": match.id,
        "competitor_item_id": match.competitor_item_id,
        "competitor": item.competitor if item else None,
        "external_id": item.external_id if item else None,
        "competitor_name": item.name if item else None,
        "product_id": match.product_id,
        "product_article": product.article if product else None,
        "product_name": product.name if product else None,
        "score": _match_score(match),
    }


def accept_suggestions(
    session: Session,
    *,
    first_seen_after: str | None = None,
    min_score: float | None = None,
    limit: int | None = None,
    dry_run: bool = True,
    batch_id: str | None = None,
    sample_limit: int = 50,
) -> dict[str, Any]:
    first_seen_date = _parse_date(first_seen_after)
    query = (
        select(CompetitorItemMatch)
        .options(
            joinedload(CompetitorItemMatch.competitor_item),
            joinedload(CompetitorItemMatch.product),
        )
        .join(CompetitorItem, CompetitorItem.id == CompetitorItemMatch.competitor_item_id)
        .where(
            CompetitorItemMatch.status == CompetitorItemMatchStatus.SUGGESTED,
            CompetitorItemMatch.method != CompetitorItemMatchMethod.MANUAL,
        )
        .order_by(CompetitorItem.competitor, CompetitorItem.external_id)
    )
    if first_seen_date:
        query = query.where(func.date(CompetitorItem.first_seen_at) >= first_seen_date)
    if limit:
        query = query.limit(limit)

    matches = list(session.execute(query).scalars())
    selected: list[CompetitorItemMatch] = []
    skipped_low_score = 0
    for match in matches:
        score = _match_score(match)
        if min_score is not None and (score is None or score < min_score):
            skipped_low_score += 1
            continue
        selected.append(match)

    now = datetime.now(UTC)
    accept_batch_id = batch_id or f"bulk_accept_suggested_{now:%Y%m%d_%H%M%S}"
    samples = [_sample_row(match) for match in selected[:sample_limit]]

    if not dry_run:
        for match in selected:
            rationale = dict(match.rationale_json or {})
            rationale["bulk_accept_suggested"] = {
                "batch_id": accept_batch_id,
                "accepted_at": now.isoformat(),
                "first_seen_after": first_seen_after,
                "min_score": min_score,
                "previous_status": CompetitorItemMatchStatus.SUGGESTED.value,
            }
            match.status = CompetitorItemMatchStatus.ACCEPTED
            match.rationale_json = rationale
            match.updated_at = now
            session.add(match)
        session.commit()

    return {
        "dry_run": dry_run,
        "batch_id": accept_batch_id,
        "first_seen_after": first_seen_after,
        "min_score": min_score,
        "seen_suggested": len(matches),
        "accepted": len(selected) if not dry_run else 0,
        "would_accept": len(selected),
        "skipped_low_score": skipped_low_score,
        "samples": samples,
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Bulk-accept generated suggested matches")
    parser.add_argument(
        "--first-seen-after", help="Filter by competitor first_seen_at >= YYYY-MM-DD"
    )
    parser.add_argument("--min-score", type=float, help="Optional minimum match score")
    parser.add_argument("--limit", type=int, help="Limit selected suggestions")
    parser.add_argument("--apply", action="store_true", help="Apply changes; default is dry-run")
    parser.add_argument("--batch-id", help="Audit batch id")
    parser.add_argument("--sample-limit", type=int, default=50)
    parser.add_argument("--report-file", help="Write JSON report")
    args = parser.parse_args()

    settings = get_settings()
    engine = build_engine(settings.database_url)
    with Session(engine) as session:
        payload = accept_suggestions(
            session,
            first_seen_after=args.first_seen_after,
            min_score=args.min_score,
            limit=args.limit,
            dry_run=not args.apply,
            batch_id=args.batch_id,
            sample_limit=args.sample_limit,
        )

    if args.report_file:
        _write_json(Path(args.report_file), payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
