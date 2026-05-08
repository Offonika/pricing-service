"""Move unsafe auto-accepted competitor item matches back to review."""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, exists, func, or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import CompetitorItem, Product
from app.models.competitor_item_compatibility import CompetitorItemCompatibility
from app.models.competitor_item_match import (
    CompetitorItemMatch,
    CompetitorItemMatchMethod,
    CompetitorItemMatchStatus,
)
from app.services.matching_guardrails import basic_candidate_guardrails


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def move_unsafe_matches_to_review(
    session: Session,
    *,
    first_seen_after: date | None,
    all_accepted_without_compat: bool,
    dry_run: bool,
    sample_limit: int,
) -> dict:
    has_compat = exists().where(CompetitorItemCompatibility.competitor_item_id == CompetitorItem.id)
    conditions = [
        CompetitorItemMatch.status == CompetitorItemMatchStatus.ACCEPTED,
        CompetitorItemMatch.method != CompetitorItemMatchMethod.MANUAL,
    ]
    if all_accepted_without_compat:
        conditions.append(~has_compat)
    else:
        if first_seen_after is None:
            raise ValueError("first_seen_after is required unless all_accepted_without_compat=true")
        conditions.extend(
            [
                func.date(CompetitorItem.first_seen_at) >= first_seen_after,
                or_(CompetitorItem.attrs_json.is_(None), ~has_compat),
            ]
        )
    rows = session.execute(
        select(CompetitorItemMatch, CompetitorItem, Product)
        .join(CompetitorItem, CompetitorItem.id == CompetitorItemMatch.competitor_item_id)
        .join(Product, Product.id == CompetitorItemMatch.product_id)
        .where(*conditions)
        .order_by(CompetitorItemMatch.id.desc())
    ).all()
    now = datetime.now(timezone.utc)
    samples: list[dict] = []
    updated = 0
    for match, item, product in rows:
        guardrail = basic_candidate_guardrails(item, product)
        reason = "missing_attrs_or_compatibility"
        if not guardrail.allowed:
            reason = guardrail.reason or reason
        if len(samples) < sample_limit:
            samples.append(
                {
                    "competitor_item_id": item.id,
                    "competitor": item.competitor,
                    "external_id": item.external_id,
                    "name": item.name,
                    "product_id": product.id,
                    "product_article": product.article,
                    "product_name": product.name,
                    "final_score": (
                        float(match.final_score) if match.final_score is not None else None
                    ),
                    "reason": reason,
                }
            )
        if dry_run:
            continue
        rationale = dict(match.rationale_json or {})
        rationale["unsafe_auto_accept_review"] = {
            "reason": reason,
            "reviewed_at": now.isoformat(),
            "first_seen_after": first_seen_after.isoformat() if first_seen_after else None,
            "scope": (
                "all_accepted_without_compat"
                if all_accepted_without_compat
                else "new_missing_attrs_or_compatibility"
            ),
        }
        match.status = CompetitorItemMatchStatus.NEEDS_REVIEW
        match.rationale_json = rationale
        match.updated_at = now
        session.add(match)
        updated += 1
    if not dry_run:
        session.commit()
    return {
        "processed": len(rows),
        "updated": updated,
        "dry_run": dry_run,
        "samples": samples,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--first-seen-after", default="2026-05-01")
    parser.add_argument(
        "--all-accepted-without-compat",
        action="store_true",
        help="Review every non-manual accepted match without compatibility, regardless of first_seen_at",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--sample-limit", type=int, default=50)
    parser.add_argument("--report-file")
    args = parser.parse_args()

    settings = get_settings()
    engine = create_engine(settings.database_url)
    with Session(engine) as session:
        result = move_unsafe_matches_to_review(
            session,
            first_seen_after=_parse_date(args.first_seen_after) if args.first_seen_after else None,
            all_accepted_without_compat=args.all_accepted_without_compat,
            dry_run=args.dry_run,
            sample_limit=args.sample_limit,
        )
    if args.report_file:
        path = Path(args.report_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
