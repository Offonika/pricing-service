from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from app.models import CompetitorItem, CompetitorItemMatch, Product
from app.services.matching_guardrails import basic_candidate_guardrails

SNAPSHOT_SCHEMA_VERSION = 1
LEGACY_REASON_CODE = "legacy_unspecified"
DECISION_REASON_CODES = frozenset(
    {
        LEGACY_REASON_CODE,
        "wrong_model",
        "wrong_item_type",
        "wrong_quality",
        "wrong_color",
        "wrong_frame",
        "wrong_part_number",
        "wrong_capacity",
        "duplicate_or_irrelevant",
        "confirmed_exact_code",
        "confirmed_attributes",
        "auto_false_positive",
        "other",
    }
)


def normalize_reason_code(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in DECISION_REASON_CODES else LEGACY_REASON_CODE


def build_decision_snapshot(
    *,
    product: Product,
    item: CompetitorItem,
    match: CompetitorItemMatch | None,
    reason_code: str,
) -> dict[str, Any]:
    rationale = dict(match.rationale_json or {}) if match else {}
    guardrail = basic_candidate_guardrails(item, product)
    candidates = _candidate_snapshot(rationale, product=product, match=match)
    return _json_safe(
        {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "reason_code": normalize_reason_code(reason_code),
            "selected_rank": _selected_rank(candidates, product.id),
            "selected": {
                "product_id": product.id,
                "competitor_item_id": item.id,
                "competitor": item.competitor,
                "competitor_external_id": item.external_id,
            },
            "scores": {
                "embed_best": getattr(match, "score_embed_best", None),
                "embed_gap": getattr(match, "score_embed_gap", None),
                "llm": getattr(match, "score_llm", None),
                "final": getattr(match, "final_score", None),
            },
            "top_k": {
                "used": getattr(match, "topk_used", None),
                "candidates": candidates,
            },
            "features": {
                "product": _fields(
                    product,
                    "name",
                    "article",
                    "brand",
                    "category",
                    "subject",
                    "quality",
                    "color",
                    "battery_capacity_mah",
                    "part_type",
                    "flex_purpose",
                ),
                "competitor_item": _fields(
                    item,
                    "name",
                    "normalized_title",
                    "item_type",
                    "item_brand",
                    "attrs_model",
                    "attrs_quality",
                    "attrs_color",
                    "attrs_json",
                    "llm_confidence",
                    "parse_status",
                ),
            },
            "versions": {
                "embed_model": getattr(match, "embed_model", None),
                "embed_dim": getattr(match, "embed_dim", None),
                "parser": getattr(item, "parse_version", None),
                "guardrails": "matching_guardrails_v1",
            },
            "source_freshness": _fields(
                item,
                "first_seen_at",
                "last_seen_at",
                "scraped_at",
                "updated_at",
            ),
            "guardrail": {"allowed": guardrail.allowed, "reason": guardrail.reason},
            "rationale": rationale,
        }
    )


def snapshot_summary(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    data = snapshot or {}
    top_k = data.get("top_k") if isinstance(data.get("top_k"), dict) else {}
    candidates = top_k.get("candidates") if isinstance(top_k.get("candidates"), list) else []
    scores = data.get("scores") if isinstance(data.get("scores"), dict) else {}
    return {
        "snapshot_schema_version": data.get("schema_version"),
        "snapshot_score": scores.get("final"),
        "snapshot_rank": data.get("selected_rank"),
        "snapshot_top_k_count": len(candidates),
    }


def _candidate_snapshot(
    rationale: dict[str, Any], *, product: Product, match: CompetitorItemMatch | None
) -> list[dict[str, Any]]:
    for key in ("filtered_candidates", "top_candidates", "candidates", "alternatives"):
        value = rationale.get(key)
        if isinstance(value, list):
            return [dict(row) for row in value[:20] if isinstance(row, dict)]
    return [
        {
            "product_id": product.id,
            "article": product.article,
            "name": product.name,
            "score": getattr(match, "final_score", None),
        }
    ]


def _selected_rank(candidates: list[dict[str, Any]], product_id: int) -> int | None:
    for index, candidate in enumerate(candidates, start=1):
        if candidate.get("product_id") == product_id:
            return index
    return None


def _fields(entity: object, *names: str) -> dict[str, Any]:
    return {name: getattr(entity, name, None) for name in names}


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Enum):
        return value.value
    return value


__all__ = [
    "DECISION_REASON_CODES",
    "LEGACY_REASON_CODE",
    "SNAPSHOT_SCHEMA_VERSION",
    "build_decision_snapshot",
    "normalize_reason_code",
    "snapshot_summary",
]
