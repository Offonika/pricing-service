"""Метрики качества ночного пайплайна сопоставления конкурентов."""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime
from pathlib import Path

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.infrastructure.db.engines import build_engine
from app.models import CompetitorItem, CompetitorItemCompatibility, CompetitorItemSnapshot
from app.models.competitor_item_match import CompetitorItemMatch, CompetitorItemMatchStatus
from app.services.matching_guardrails import competitor_item_requires_compatibility

logger = logging.getLogger(__name__)


def _parse_date(value: str | None):
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d").date()


def build_report(session: Session, *, first_seen_after: str | None = None) -> dict[str, int]:
    first_seen_date = _parse_date(first_seen_after)
    base = select(CompetitorItem.id)
    if first_seen_date:
        base = base.where(func.date(CompetitorItem.first_seen_at) >= first_seen_date)
    base_subq = base.subquery()

    duplicate_groups = (
        select(
            CompetitorItemSnapshot.competitor_item_id,
            CompetitorItemSnapshot.scraped_at,
        )
        .group_by(CompetitorItemSnapshot.competitor_item_id, CompetitorItemSnapshot.scraped_at)
        .having(func.count(CompetitorItemSnapshot.id) > 1)
        .subquery()
    )

    missing_compat_items = (
        session.execute(
            select(CompetitorItem).where(
                CompetitorItem.id.in_(select(base_subq.c.id)),
                ~exists().where(
                    CompetitorItemCompatibility.competitor_item_id == CompetitorItem.id
                ),
            )
        )
        .scalars()
        .all()
    )
    missing_compat_actionable = 0
    missing_compat_ignored = 0
    missing_compat_reason_counts: dict[str, int] = {}
    for item in missing_compat_items:
        target = competitor_item_requires_compatibility(item)
        if target.requires_compatibility:
            missing_compat_actionable += 1
        else:
            missing_compat_ignored += 1
        metric_key = f"new_without_compatibility_reason_{target.reason.replace(':', '_')}"
        missing_compat_reason_counts[metric_key] = (
            missing_compat_reason_counts.get(metric_key, 0) + 1
        )

    accepted_without_compatibility_base = (
        select(func.count())
        .select_from(CompetitorItemMatch)
        .join(CompetitorItem, CompetitorItem.id == CompetitorItemMatch.competitor_item_id)
        .where(
            CompetitorItemMatch.status == CompetitorItemMatchStatus.ACCEPTED,
            ~exists().where(CompetitorItemCompatibility.competitor_item_id == CompetitorItem.id),
        )
    )
    code_overlap_auto_accept = CompetitorItemMatch.rationale_json[
        "auto_accept_explicit_model_code_overlap"
    ].is_not(None)

    metrics = {
        "new_items": session.scalar(select(func.count()).select_from(base_subq)) or 0,
        "new_without_attrs": session.scalar(
            select(func.count())
            .select_from(CompetitorItem)
            .where(
                CompetitorItem.id.in_(select(base_subq.c.id)),
                CompetitorItem.attrs_json.is_(None),
            )
        )
        or 0,
        "new_without_compatibility": missing_compat_actionable,
        "new_without_compatibility_total": len(missing_compat_items),
        "new_without_compatibility_ignored": missing_compat_ignored,
        "accepted_without_compatibility": session.scalar(
            accepted_without_compatibility_base.where(~code_overlap_auto_accept)
        )
        or 0,
        "accepted_without_compatibility_code_overlap": session.scalar(
            accepted_without_compatibility_base.where(code_overlap_auto_accept)
        )
        or 0,
        "duplicate_snapshot_groups": session.scalar(
            select(func.count()).select_from(duplicate_groups)
        )
        or 0,
        "needs_review": session.scalar(
            select(func.count()).where(
                CompetitorItemMatch.status == CompetitorItemMatchStatus.NEEDS_REVIEW
            )
        )
        or 0,
        "ambiguous": session.scalar(
            select(func.count()).where(
                CompetitorItemMatch.status == CompetitorItemMatchStatus.AMBIGUOUS
            )
        )
        or 0,
    }
    metrics.update(missing_compat_reason_counts)
    return {key: int(value) for key, value in metrics.items()}


def main() -> None:
    parser = argparse.ArgumentParser(description="Report competitor matching quality metrics")
    parser.add_argument("--first-seen-after", help="Filter new item metrics by YYYY-MM-DD")
    parser.add_argument("--report-file", help="Write JSON report to file")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = get_settings()
    engine = build_engine(settings.database_url)
    with Session(engine) as session:
        report = build_report(session, first_seen_after=args.first_seen_after)

    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report_file:
        path = Path(args.report_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    logger.info("competitor matching quality report: %s", report)


if __name__ == "__main__":
    main()
