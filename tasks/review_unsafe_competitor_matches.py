"""Move unsafe auto-accepted competitor item matches back to review."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.infrastructure.db.engines import build_engine
from app.models import CompetitorItem, Product
from app.models.competitor_item_compatibility import CompetitorItemCompatibility
from app.models.competitor_item_match import (
    CompetitorItemMatch,
    CompetitorItemMatchMethod,
    CompetitorItemMatchStatus,
)
from app.services.matching_guardrails import basic_candidate_guardrails

SAFE_AUTO_ACCEPT_RATIONALE_KEYS = {
    "auto_accept_battery_original_part_code",
    "auto_accept_battery_part_code",
    "auto_accept_connector",
    "auto_accept_display_construction",
    "auto_accept_display_matrix_tag",
    "auto_accept_display_matrix_type",
    "auto_accept_display_original_quality",
    "auto_accept_display_unspecified_quality",
    "auto_accept_explicit_model_code_overlap",
    "auto_accept_explicit_model_text",
    "auto_accept_flex",
    "auto_accept_housing_part",
    "auto_accept_iphone_battery_capacity",
    "auto_accept_camera",
    "auto_accept_other_safe_family",
}
DEFAULT_SAFE_AUTO_ACCEPT_MIN_SCORE = 0.80


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_auto_accept_rationale_key(
    match: CompetitorItemMatch,
) -> tuple[str | None, float]:
    rationale = match.rationale_json or {}
    for key in SAFE_AUTO_ACCEPT_RATIONALE_KEYS:
        details = rationale.get(key)
        if not isinstance(details, dict):
            continue
        threshold = (
            _float(details.get("query_min_score"))
            or _float(details.get("min_score"))
            or DEFAULT_SAFE_AUTO_ACCEPT_MIN_SCORE
        )
        return key, threshold
    return None, DEFAULT_SAFE_AUTO_ACCEPT_MIN_SCORE


def _safe_auto_accept_can_stay(match: CompetitorItemMatch, *, guardrail_allowed: bool) -> bool:
    if not guardrail_allowed:
        return False
    rationale_key, min_score = _safe_auto_accept_rationale_key(match)
    if not rationale_key:
        return False
    score = _float(match.final_score)
    return bool(score is not None and score >= min_score)


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
    safe_auto_accept_kept = 0
    unsafe_auto_accept_moved_to_review = 0
    updated = 0
    unsafe_reason_counts: Counter[str] = Counter()
    for match, item, product in rows:
        guardrail = basic_candidate_guardrails(item, product)
        reason = "missing_attrs_or_compatibility"
        if not guardrail.allowed:
            reason = guardrail.reason or reason
        rationale_key, safe_min_score = _safe_auto_accept_rationale_key(match)
        keep_safe_auto_accept = _safe_auto_accept_can_stay(
            match,
            guardrail_allowed=guardrail.allowed,
        )
        if keep_safe_auto_accept:
            safe_auto_accept_kept += 1
            if len(samples) < sample_limit:
                samples.append(
                    {
                        "action": "kept_safe_auto_accept",
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
                        "reason": "safe_auto_accept_rationale",
                        "rationale_key": rationale_key,
                        "safe_min_score": safe_min_score,
                    }
                )
            continue
        unsafe_auto_accept_moved_to_review += 1
        unsafe_reason_counts[reason] += 1
        if len(samples) < sample_limit:
            samples.append(
                {
                    "action": "moved_to_review",
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
                    "rationale_key": rationale_key,
                    "safe_min_score": safe_min_score,
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
        "safe_auto_accept_kept": safe_auto_accept_kept,
        "unsafe_auto_accept_moved_to_review": unsafe_auto_accept_moved_to_review,
        "unsafe_reason_counts": dict(sorted(unsafe_reason_counts.items())),
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
    engine = build_engine(settings.database_url)
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
