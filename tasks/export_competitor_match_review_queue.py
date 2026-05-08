"""Export competitor match review queue with reason codes."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, joinedload

from app.core.config import get_settings
from app.models import CompetitorItem, Product
from app.models.competitor_item_compatibility import CompetitorItemCompatibility
from app.models.competitor_item_match import CompetitorItemMatch, CompetitorItemMatchStatus
from app.services.matching_guardrails import catalog_family, competitor_item_requires_compatibility

DEFAULT_STATUSES = (
    CompetitorItemMatchStatus.SUGGESTED,
    CompetitorItemMatchStatus.NEEDS_REVIEW,
    CompetitorItemMatchStatus.AMBIGUOUS,
)


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d").date()


def _float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


def _safe_json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, list):
        return [_safe_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _safe_json_value(item) for key, item in value.items()}
    return value


def _status_value(status: CompetitorItemMatchStatus | str | None) -> str | None:
    if status is None:
        return None
    return status.value if hasattr(status, "value") else str(status)


def _candidate_summary(candidates: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for candidate in candidates[:limit]:
        rows.append(
            {
                "product_id": candidate.get("product_id"),
                "article": candidate.get("article"),
                "name": candidate.get("name"),
                "score": _float(candidate.get("score")),
            }
        )
    return rows


def _reason_codes(
    *,
    item: CompetitorItem,
    product: Product,
    match: CompetitorItemMatch,
    has_compatibility: bool,
    min_score: float,
    min_gap: float,
) -> list[str]:
    reasons: list[str] = []
    status = _status_value(match.status)
    if status:
        reasons.append(f"status:{status}")

    score = _float(match.final_score or match.score_llm or match.score_embed_best)
    gap = _float(match.score_embed_gap)
    rationale = match.rationale_json or {}
    if gap is None:
        gap = _float(rationale.get("gap"))
    if score is None:
        score = _float(rationale.get("best_score"))

    if score is not None and score < min_score:
        reasons.append("low_score")
    if gap is not None and gap < min_gap:
        reasons.append("small_gap")

    candidates = rationale.get("filtered_candidates") or []
    if len(candidates) > 1 and gap is not None and gap < min_gap * 2:
        reasons.append("multiple_close_candidates")

    if rationale.get("display_frame_review"):
        reasons.append("display_frame_review")
    if rationale.get("display_model_code_review"):
        reasons.append("display_model_code_review")
    if rationale.get("display_quality_review"):
        reasons.append("display_quality_review")

    if (
        not has_compatibility
        and competitor_item_requires_compatibility(item).requires_compatibility
    ):
        reasons.append("missing_compatibility")

    item_family = catalog_family(
        " ".join(
            value
            for value in (item.name, item.normalized_title, item.category, item.category_group)
            if value
        )
    )
    product_family = catalog_family(
        " ".join(value for value in (product.name, product.category, product.subject) if value)
    )
    if item_family and product_family and item_family != product_family:
        reasons.append(f"family_mismatch:{item_family}->{product_family}")

    return list(dict.fromkeys(reasons))


def _review_row(
    *,
    item: CompetitorItem,
    product: Product,
    match: CompetitorItemMatch,
    has_compatibility: bool,
    min_score: float,
    min_gap: float,
    alternatives_limit: int,
) -> dict[str, Any]:
    rationale = match.rationale_json or {}
    candidates = _candidate_summary(
        rationale.get("filtered_candidates") or [],
        limit=alternatives_limit,
    )
    score = _float(match.final_score or match.score_llm or match.score_embed_best)
    gap = _float(match.score_embed_gap)
    if score is None:
        score = _float(rationale.get("best_score"))
    if gap is None:
        gap = _float(rationale.get("gap"))

    return {
        "match_id": match.id,
        "status": _status_value(match.status),
        "method": match.method.value if hasattr(match.method, "value") else str(match.method),
        "reason_codes": _reason_codes(
            item=item,
            product=product,
            match=match,
            has_compatibility=has_compatibility,
            min_score=min_score,
            min_gap=min_gap,
        ),
        "score": score,
        "gap": gap,
        "competitor_item_id": item.id,
        "competitor": item.competitor,
        "external_id": item.external_id,
        "competitor_name": item.name,
        "competitor_item_type": item.item_type,
        "competitor_category": item.category,
        "competitor_category_group": item.category_group,
        "competitor_brand": item.item_brand or item.parsed_device_brand,
        "competitor_model": item.attrs_model or item.parsed_device_model,
        "competitor_variant": item.attrs_variant or item.parsed_device_variant,
        "competitor_color": item.attrs_color or item.color,
        "competitor_quality": item.attrs_quality or item.screen_quality_grade,
        "competitor_first_seen_at": _iso(item.first_seen_at),
        "competitor_last_seen_at": _iso(item.last_seen_at),
        "has_compatibility": has_compatibility,
        "product_id": product.id,
        "product_article": product.article,
        "product_name": product.name,
        "product_brand": product.brand,
        "product_category": product.category,
        "product_subject": product.subject or product.subject_1c or product.subject_generated,
        "product_color": product.color,
        "product_quality": product.quality or product.display_quality,
        "product_display_type": product.display_type,
        "product_display_has_frame": product.display_has_frame,
        "alternatives": candidates,
    }


def build_review_queue(
    session: Session,
    *,
    first_seen_after: str | None = None,
    statuses: list[str] | None = None,
    limit: int | None = None,
    min_score: float = 0.80,
    min_gap: float = 0.02,
    alternatives_limit: int = 5,
) -> dict[str, Any]:
    first_seen_date = _parse_date(first_seen_after)
    status_values = statuses or [status.value for status in DEFAULT_STATUSES]
    enum_statuses = [CompetitorItemMatchStatus(value) for value in status_values]

    query = (
        select(CompetitorItemMatch)
        .options(
            joinedload(CompetitorItemMatch.competitor_item),
            joinedload(CompetitorItemMatch.product),
        )
        .join(CompetitorItem, CompetitorItem.id == CompetitorItemMatch.competitor_item_id)
        .where(CompetitorItemMatch.status.in_(enum_statuses))
    )
    if first_seen_date:
        query = query.where(func.date(CompetitorItem.first_seen_at) >= first_seen_date)
    query = query.order_by(
        CompetitorItemMatch.status,
        CompetitorItem.competitor,
        CompetitorItem.external_id,
    )

    matches = session.execute(query).scalars().all()
    item_ids = [match.competitor_item_id for match in matches]
    item_ids_with_compatibility: set[int] = set()
    if item_ids:
        item_ids_with_compatibility = set(
            session.execute(
                select(CompetitorItemCompatibility.competitor_item_id)
                .where(CompetitorItemCompatibility.competitor_item_id.in_(item_ids))
                .distinct()
            ).scalars()
        )

    rows = [
        _review_row(
            item=match.competitor_item,
            product=match.product,
            match=match,
            has_compatibility=match.competitor_item_id in item_ids_with_compatibility,
            min_score=min_score,
            min_gap=min_gap,
            alternatives_limit=alternatives_limit,
        )
        for match in matches
        if match.competitor_item and match.product
    ]
    status_priority = {"needs_review": 0, "ambiguous": 1, "suggested": 2}
    rows.sort(
        key=lambda row: (
            status_priority.get(str(row["status"]), 9),
            -len(row["reason_codes"]),
            -(row["score"] or 0),
            row["competitor"] or "",
            row["external_id"] or "",
        )
    )
    if limit:
        rows = rows[:limit]

    reason_counts = Counter(reason for row in rows for reason in row["reason_codes"])
    status_counts = Counter(str(row["status"]) for row in rows)
    return {
        "total": len(rows),
        "status_counts": dict(status_counts),
        "reason_counts": dict(reason_counts),
        "items": rows,
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_safe_json_value(payload), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _csv_row(row: dict[str, Any], *, alternatives_limit: int) -> dict[str, Any]:
    flat = dict(row)
    flat["reason_codes"] = ";".join(row.get("reason_codes") or [])
    alternatives = row.get("alternatives") or []
    flat.pop("alternatives", None)
    for index in range(alternatives_limit):
        alt = alternatives[index] if index < len(alternatives) else {}
        prefix = f"alt{index + 1}"
        flat[f"{prefix}_product_id"] = alt.get("product_id")
        flat[f"{prefix}_article"] = alt.get("article")
        flat[f"{prefix}_name"] = alt.get("name")
        flat[f"{prefix}_score"] = alt.get("score")
    return _safe_json_value(flat)


def _write_csv(path: Path, rows: list[dict[str, Any]], *, alternatives_limit: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    base_fields = [
        "match_id",
        "status",
        "method",
        "reason_codes",
        "score",
        "gap",
        "competitor_item_id",
        "competitor",
        "external_id",
        "competitor_name",
        "competitor_item_type",
        "competitor_category",
        "competitor_category_group",
        "competitor_brand",
        "competitor_model",
        "competitor_variant",
        "competitor_color",
        "competitor_quality",
        "competitor_first_seen_at",
        "competitor_last_seen_at",
        "has_compatibility",
        "product_id",
        "product_article",
        "product_name",
        "product_brand",
        "product_category",
        "product_subject",
        "product_color",
        "product_quality",
        "product_display_type",
        "product_display_has_frame",
    ]
    alt_fields = [
        field
        for index in range(alternatives_limit)
        for field in (
            f"alt{index + 1}_product_id",
            f"alt{index + 1}_article",
            f"alt{index + 1}_name",
            f"alt{index + 1}_score",
        )
    ]
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=[*base_fields, *alt_fields])
        writer.writeheader()
        for row in rows:
            writer.writerow(_csv_row(row, alternatives_limit=alternatives_limit))


def main() -> None:
    parser = argparse.ArgumentParser(description="Export competitor match review queue")
    parser.add_argument(
        "--first-seen-after", help="Filter by competitor first_seen_at >= YYYY-MM-DD"
    )
    parser.add_argument(
        "--status",
        action="append",
        choices=[status.value for status in DEFAULT_STATUSES],
        help="Match status to include; can be passed multiple times",
    )
    parser.add_argument("--limit", type=int, help="Limit rows")
    parser.add_argument("--min-score", type=float, default=0.80)
    parser.add_argument("--min-gap", type=float, default=0.02)
    parser.add_argument("--alternatives-limit", type=int, default=5)
    parser.add_argument("--report-file", help="Write JSON report")
    parser.add_argument("--report-csv", help="Write CSV report")
    args = parser.parse_args()

    settings = get_settings()
    engine = create_engine(settings.database_url)
    with Session(engine) as session:
        payload = build_review_queue(
            session,
            first_seen_after=args.first_seen_after,
            statuses=args.status,
            limit=args.limit,
            min_score=args.min_score,
            min_gap=args.min_gap,
            alternatives_limit=args.alternatives_limit,
        )

    if args.report_file:
        _write_json(Path(args.report_file), payload)
    if args.report_csv:
        _write_csv(
            Path(args.report_csv), payload["items"], alternatives_limit=args.alternatives_limit
        )
    print(json.dumps(_safe_json_value(payload), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
