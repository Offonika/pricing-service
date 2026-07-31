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

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.infrastructure.db import session_scope
from app.models import CompetitorItem, Product, ProductCompetitorItemDecision
from app.models.competitor_item_compatibility import CompetitorItemCompatibility
from app.models.competitor_item_match import CompetitorItemMatch, CompetitorItemMatchStatus
from app.services.matching_guardrails import catalog_family, competitor_item_requires_compatibility
from tasks.match_competitor_items_embeddings import _effective_item_type

DEFAULT_STATUSES = (
    CompetitorItemMatchStatus.SUGGESTED,
    CompetitorItemMatchStatus.NEEDS_REVIEW,
    CompetitorItemMatchStatus.AMBIGUOUS,
)

DISPLAY_REVIEW_REASONS = {
    "display_frame_review",
    "display_model_code_review",
    "display_quality_review",
}

DOMAIN_SUGGEST_RATIONALE_KEYS = {
    "battery_part_code_model_suggest",
    "battery_verification_suggest",
    "disposable_battery_suggest",
    "flex_suggest",
    "housing_part_suggest",
    "iphone_battery_model_capacity_suggest",
    "network_cable_suggest",
    "phone_camera_glass_suggest",
    "phone_sim_tray_suggest",
    "screen_protector_suggest",
    "stencil_suggest",
}


def _has_reason_prefix(reasons: list[str], prefix: str) -> bool:
    return any(reason.startswith(prefix) for reason in reasons)


def _has_domain_suggest_rationale(rationale: dict[str, Any]) -> bool:
    return bool(DOMAIN_SUGGEST_RATIONALE_KEYS.intersection(rationale))


def _review_bucket(*, reasons: list[str], item_type: str | None) -> str:
    reason_set = set(reasons)
    normalized_item_type = (item_type or "").lower()
    if "missing_compatibility" in reason_set:
        return "compatibility_or_family"
    if normalized_item_type in {"other", "cable"}:
        return "other_low_priority"
    if _has_reason_prefix(reasons, "family_mismatch:"):
        return "compatibility_or_family"
    if reason_set.intersection(DISPLAY_REVIEW_REASONS):
        return "display_attributes"
    if "small_gap" in reason_set:
        return "candidate_tie"
    if "multiple_close_candidates" in reason_set and "low_score" not in reason_set:
        return "candidate_tie"
    if "low_score" in reason_set:
        return "low_score"
    return "general_review"


def _review_priority(bucket: str) -> int:
    return {
        "compatibility_or_family": 1,
        "display_attributes": 2,
        "candidate_tie": 3,
        "low_score": 4,
        "general_review": 5,
        "other_low_priority": 6,
    }.get(bucket, 9)


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
    domain_suggested = status == "suggested" and _has_domain_suggest_rationale(rationale)

    if score is not None and score < min_score:
        reasons.append("low_score")
    if gap is not None and gap < min_gap and not domain_suggested:
        reasons.append("small_gap")

    candidates = rationale.get("filtered_candidates") or []
    if len(candidates) > 1 and gap is not None and gap < min_gap * 2 and not domain_suggested:
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
    training_examples: int,
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
    reasons = _reason_codes(
        item=item,
        product=product,
        match=match,
        has_compatibility=has_compatibility,
        min_score=min_score,
        min_gap=min_gap,
    )
    effective_item_type = _effective_item_type(item)
    bucket = _review_bucket(reasons=reasons, item_type=effective_item_type)
    stock_quantity = int(product.stock.quantity) if product.stock else 0
    uncertainty_score = round(
        (1.0 - min(max(score or 0.0, 0.0), 1.0))
        + (0.5 if gap is None else max(0.0, min_gap - gap) * 10)
        + (0.25 if _status_value(match.status) == "ambiguous" else 0.0),
        4,
    )
    business_value_score = round(
        min(max(stock_quantity, 0), 100) / 100
        + (0.75 if item.availability else 0.0)
        + (0.25 if item.price_roz is not None or item.price_opt is not None else 0.0),
        4,
    )
    training_scarcity_score = round(1.0 / (1 + training_examples), 4)
    family_label = (
        item.attrs_model
        or item.parsed_device_model
        or item.category_group
        or item.category
        or catalog_family(item.name or item.normalized_title or "")
        or "unknown"
    )
    family_group = ":".join(
        (item.competitor or "unknown", effective_item_type or "unknown", str(family_label).lower())
    )
    queue_priority_score = round(
        business_value_score * 3 + uncertainty_score * 2 + training_scarcity_score,
        4,
    )

    return {
        "match_id": match.id,
        "status": _status_value(match.status),
        "method": match.method.value if hasattr(match.method, "value") else str(match.method),
        "review_bucket": bucket,
        "review_priority": _review_priority(bucket),
        "queue_priority_score": queue_priority_score,
        "business_value_score": business_value_score,
        "uncertainty_score": uncertainty_score,
        "training_examples": training_examples,
        "training_scarcity_score": training_scarcity_score,
        "family_group": family_group,
        "reason_codes": reasons,
        "score": score,
        "gap": gap,
        "competitor_item_id": item.id,
        "competitor": item.competitor,
        "external_id": item.external_id,
        "competitor_name": item.name,
        "competitor_item_type": effective_item_type,
        "competitor_raw_item_type": item.item_type,
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
            joinedload(CompetitorItemMatch.product).joinedload(Product.stock),
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
    training_counts: Counter[tuple[str, str]] = Counter()
    training_items = session.execute(
        select(CompetitorItem)
        .join(
            ProductCompetitorItemDecision,
            ProductCompetitorItemDecision.competitor_item_id == CompetitorItem.id,
        )
        .where(ProductCompetitorItemDecision.action.in_(("accept", "reject", "revoke")))
    ).scalars()
    for training_item in training_items:
        training_counts[
            (
                (training_item.competitor or "unknown").lower(),
                _effective_item_type(training_item) or "unknown",
            )
        ] += 1
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
            training_examples=training_counts[
                (
                    (match.competitor_item.competitor or "unknown").lower(),
                    _effective_item_type(match.competitor_item) or "unknown",
                )
            ],
        )
        for match in matches
        if match.competitor_item and match.product
    ]
    status_priority = {"needs_review": 0, "ambiguous": 1, "suggested": 2}
    grouped_rows: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped_rows.setdefault(str(row["family_group"]), []).append(row)
    ordered_groups = sorted(
        grouped_rows.values(),
        key=lambda group: (
            -max(float(row["queue_priority_score"]) for row in group),
            min(int(row["review_priority"]) for row in group),
            str(group[0]["family_group"]),
        ),
    )
    rows = []
    for group in ordered_groups:
        group.sort(
            key=lambda row: (
                -float(row["queue_priority_score"]),
                status_priority.get(str(row["status"]), 9),
                -len(row["reason_codes"]),
                -(row["score"] or 0),
                row["external_id"] or "",
            )
        )
        rows.extend(group)
    if limit:
        rows = rows[:limit]

    reason_counts = Counter(reason for row in rows for reason in row["reason_codes"])
    status_counts = Counter(str(row["status"]) for row in rows)
    bucket_counts = Counter(str(row["review_bucket"]) for row in rows)
    priority_counts = Counter(str(row["review_priority"]) for row in rows)
    family_counts = Counter(str(row["family_group"]) for row in rows)
    return {
        "total": len(rows),
        "status_counts": dict(status_counts),
        "bucket_counts": dict(bucket_counts),
        "priority_counts": dict(priority_counts),
        "family_counts": dict(family_counts),
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
        "review_bucket",
        "review_priority",
        "queue_priority_score",
        "business_value_score",
        "uncertainty_score",
        "training_examples",
        "training_scarcity_score",
        "family_group",
        "reason_codes",
        "score",
        "gap",
        "competitor_item_id",
        "competitor",
        "external_id",
        "competitor_name",
        "competitor_item_type",
        "competitor_raw_item_type",
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

    with session_scope(read_only=True) as session:
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
