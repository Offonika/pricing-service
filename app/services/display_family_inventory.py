"""Pure read-only proposal builder for a versioned display-family registry."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Mapping, Sequence

from app.models import Product
from app.services.display_identity import DisplayIdentity, display_identity_for_product
from app.services.display_scope_policy import (
    display_scope_exclusion_reason,
    filter_display_scope_records,
)

DISPLAY_FAMILY_INVENTORY_SCHEMA = "display_family_inventory.v2"

_EXPLICIT_DISPLAY_MODULE_RE = re.compile(
    r"^\s*(?:диспле(?:й|и)\b|lcd\s+диспле(?:й|и)\b|display\b|"
    r"модул(?:ь|и)\s+диспле(?:я|ев)\b|экран\b)",
    re.IGNORECASE,
)
_EXPLICIT_NON_DISPLAY_RE = re.compile(
    r"^\s*(?:"
    r"рамк|стекл|защитн\w*\s+стекл|тачскрин|сенсор|шлейф|сумк|чехол|"
    r"джойстик|джостик|(?:нижн\w*\s+)?плат(?:а|ы|у|е|ой)\b|"
    r"микросхем|раз[ъь]ем|держател|"
    r"лоток|подсветк|тестер|"
    r"тестов|оснастк|станок|программатор|монитор|аккумулятор|корпус|"
    r"задн\w*\s+крышк|винт|проклейк|камер|динамик|микрофон|кнопк|антенн"
    r")",
    re.IGNORECASE,
)
_CONNECTIVITY_VARIANT_RE = re.compile(
    r"(?<![a-z])(?:e?sim|dual\s*sim)(?![a-z])",
    re.IGNORECASE,
)
_EXPECTED_DEVICE_VARIANTS = {
    "air",
    "fe",
    "lite",
    "max",
    "mini",
    "plus",
    "pro",
    "pro max",
    "ultra",
}


def _clean(value: object | None) -> str:
    return " ".join(str(value or "").strip().split())


def _subtract_months(value: date, months: int) -> date:
    if months <= 0:
        raise ValueError("history_months must be positive")
    month_index = value.year * 12 + value.month - 1 - months
    year, month_offset = divmod(month_index, 12)
    month = month_offset + 1
    month_lengths = (
        31,
        29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
        31,
        30,
        31,
        30,
        31,
        31,
        30,
        31,
        30,
        31,
    )
    return date(year, month, min(value.day, month_lengths[month - 1]))


def _stable_id(prefix: str, values: Sequence[object]) -> str:
    normalized = "\0".join(_clean(value).casefold() for value in values if _clean(value))
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def _product_code(product: Product) -> str:
    return _clean(product.code_1c or product.article or product.fact_sku or product.id)


@dataclass(frozen=True)
class DisplayProductClassification:
    included: bool
    reason: str
    warnings: tuple[str, ...] = ()


def classify_display_family_product(product: Product) -> DisplayProductClassification:
    """Classify a display module without trusting one contradictory taxonomy field."""

    name = _clean(product.name).casefold().replace("ё", "е")
    scope_exclusion = display_scope_exclusion_reason(name)
    if scope_exclusion:
        return DisplayProductClassification(included=False, reason=scope_exclusion)
    subjects = tuple(
        normalized
        for value in (product.subject, product.subject_1c, product.subject_generated)
        if (normalized := _clean(value).casefold().replace("ё", "е")) and normalized != "неизвестно"
    )
    category = _clean(product.category).casefold().replace("ё", "е")
    exact_subject = any(value in {"дисплей", "display"} for value in subjects)
    display_category = category == "дисплеи" or category.startswith("дисплеи для ")
    explicit_display = bool(_EXPLICIT_DISPLAY_MODULE_RE.search(name))
    explicit_non_display = bool(_EXPLICIT_NON_DISPLAY_RE.search(name))

    if explicit_non_display:
        warnings = ("display_taxonomy_conflict",) if exact_subject or display_category else ()
        return DisplayProductClassification(
            included=False,
            reason="excluded_non_display_name",
            warnings=warnings,
        )
    if explicit_display:
        authoritative_subject = subjects[0] if subjects else ""
        taxonomy_conflict = bool(
            authoritative_subject and authoritative_subject not in {"дисплей", "display"}
        )
        return DisplayProductClassification(
            included=True,
            reason="explicit_display_module_name",
            warnings=("display_taxonomy_conflict",) if taxonomy_conflict else (),
        )
    if exact_subject or display_category:
        return DisplayProductClassification(
            included=True,
            reason="taxonomy_display_without_explicit_name",
            warnings=("display_name_ambiguous",),
        )
    return DisplayProductClassification(included=False, reason="non_display_taxonomy")


def is_display_family_product(product: Product) -> bool:
    """Compatibility predicate backed by the auditable v2 classifier."""

    return classify_display_family_product(product).included


@dataclass(frozen=True)
class DisplayInventoryScopeEvidence:
    last_sale_at: date | None = None
    current_stock_qty: Decimal = Decimal("0")
    has_recent_or_open_order: bool = False


@dataclass(frozen=True)
class AcceptedCompetitorMatchEvidence:
    competitor_item_id: int
    competitor: str
    competitor_name: str
    method: str
    identity: DisplayIdentity


def _quantity_output(value: Decimal) -> int | str:
    if value == value.to_integral_value():
        return int(value)
    return format(value.normalize(), "f")


def _model_relation(ours: DisplayIdentity, theirs: DisplayIdentity) -> str:
    our_ids = set(ours.phone_model_ids)
    their_ids = set(theirs.phone_model_ids)
    if not their_ids:
        return "competitor_model_unresolved"
    if not our_ids:
        return "our_model_unresolved"
    if our_ids == their_ids:
        return "exact_model_ids"
    if our_ids & their_ids:
        return "partial_model_ids"
    if set(ours.model_keys) & set(theirs.model_keys):
        return "normalized_model_key_overlap"
    return "disjoint_model_ids"


def _property_disagreements(
    ours: DisplayIdentity,
    theirs: DisplayIdentity,
) -> list[dict[str, Any]]:
    comparisons = {
        "quality": (ours.quality_segment, theirs.quality_segment, "unknown"),
        "construction": (
            ours.construction_segment,
            theirs.construction_segment,
            "unknown",
        ),
        "frame": (ours.has_frame, theirs.has_frame, None),
        "ic_pad": (ours.has_ic_pad, theirs.has_ic_pad, None),
    }
    disagreements: list[dict[str, Any]] = []
    for field, (our_value, competitor_value, unknown) in comparisons.items():
        if our_value == unknown or competitor_value == unknown:
            continue
        if our_value != competitor_value:
            disagreements.append(
                {
                    "field": field,
                    "our_value": our_value,
                    "competitor_value": competitor_value,
                }
            )
    return disagreements


def _matching_audit(
    identity: DisplayIdentity,
    evidence: Sequence[AcceptedCompetitorMatchEvidence],
) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    relation_counts: Counter[str] = Counter()
    property_counts: Counter[str] = Counter()
    manual_review = False
    review = False
    for item in sorted(evidence, key=lambda value: value.competitor_item_id):
        relation = _model_relation(identity, item.identity)
        disagreements = _property_disagreements(identity, item.identity)
        needs_review = relation != "exact_model_ids" or bool(disagreements)
        review = review or needs_review
        manual_review = manual_review or (item.method == "manual" and needs_review)
        relation_counts[relation] += 1
        property_counts.update(value["field"] for value in disagreements)
        matches.append(
            {
                "competitor_item_id": item.competitor_item_id,
                "competitor": item.competitor,
                "competitor_name": item.competitor_name,
                "method": item.method,
                "model_relation": relation,
                "property_disagreements": disagreements,
                "competitor_segment_id": item.identity.segment_id,
            }
        )
    warnings: list[str] = []
    if review:
        warnings.append("accepted_matching_review")
    if manual_review:
        warnings.append("manual_accepted_matching_review")
    return {
        "accepted_count": len(matches),
        "manual_accepted_count": sum(item.method == "manual" for item in evidence),
        "relation_counts": dict(sorted(relation_counts.items())),
        "property_disagreement_counts": dict(sorted(property_counts.items())),
        "requires_review": review,
        "warnings": warnings,
        "matches": matches,
    }


def scope_reasons(
    product: Product,
    *,
    evidence: DisplayInventoryScopeEvidence,
    cutoff: date,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if bool(product.is_active) and not bool(product.is_marked_for_deletion):
        reasons.append("active_catalog")
    if evidence.last_sale_at is not None and evidence.last_sale_at >= cutoff:
        reasons.append("sale_within_history_window")
    if evidence.current_stock_qty > 0:
        reasons.append("current_stock")
    if evidence.has_recent_or_open_order:
        reasons.append("recent_or_open_order")
    return tuple(reasons)


def _identity_row(
    *,
    product: Product,
    identity: DisplayIdentity,
    reasons: Sequence[str],
    evidence: DisplayInventoryScopeEvidence,
    classification: DisplayProductClassification,
    matching_evidence: Sequence[AcceptedCompetitorMatchEvidence],
) -> dict[str, Any]:
    matching_audit = _matching_audit(identity, matching_evidence)
    return {
        "product_id": product.id,
        "nomenclature_code": identity.code or _product_code(product),
        "article": product.article,
        "name": identity.name,
        "is_active": bool(product.is_active),
        "is_marked_for_deletion": bool(product.is_marked_for_deletion),
        "scope_reasons": list(reasons),
        "scope_classification_reason": classification.reason,
        "scope_classification_warnings": list(classification.warnings),
        "last_sale_at": evidence.last_sale_at.isoformat() if evidence.last_sale_at else None,
        "current_stock_qty": _quantity_output(evidence.current_stock_qty),
        "has_recent_or_open_order": evidence.has_recent_or_open_order,
        "phone_model_ids": list(identity.phone_model_ids),
        "phone_models": [
            {
                "id": model.phone_model_id,
                "brand": model.brand,
                "model_name": model.model_name,
                "variant": model.variant,
                "source": model.source,
                "is_manual": model.is_manual,
                "confidence": model.confidence,
            }
            for model in identity.phone_models
        ],
        "model_keys": list(identity.model_keys),
        "physical_model_signature": list(identity.physical_model_signature),
        "related_model_signature": list(identity.related_model_signature),
        "quality": identity.quality,
        "display_type": identity.display_type,
        "construction": identity.construction,
        "has_frame": identity.has_frame,
        "has_ic_pad": identity.has_ic_pad,
        "has_binding_no_solder": identity.has_binding_no_solder,
        "color": identity.color,
        "quality_segment": identity.quality_segment,
        "construction_segment": identity.construction_segment,
        "segment_id": identity.segment_id,
        "identity_warnings": list(identity.warnings),
        "identity_schema_version": identity.schema_version,
        "identity_rules_version": identity.rules_version,
        "identity_evidence": identity.evidence,
        "matching_audit": matching_audit,
        "available_at_status": "current_snapshot_only",
    }


def _normalized_variants(rows: Sequence[dict[str, Any]]) -> set[str]:
    return {
        _clean(model.get("variant")).casefold()
        for row in rows
        for model in row.get("phone_models", ())
        if _clean(model.get("variant"))
    }


def _has_connectivity_variant(row: Mapping[str, Any]) -> bool:
    values = [str(row.get("name") or "")]
    values.extend(str(model.get("variant") or "") for model in row.get("phone_models", ()))
    return bool(_CONNECTIVITY_VARIANT_RE.search(" ".join(values)))


def _annotate_family_candidates(rows: list[dict[str, Any]]) -> None:
    by_signature: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    related_signatures: dict[tuple[str, ...], set[tuple[str, ...]]] = defaultdict(set)
    signatures_by_model: dict[str, set[tuple[str, ...]]] = defaultdict(set)
    rows_by_related: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)

    for row in rows:
        signature = tuple(row["physical_model_signature"])
        related = tuple(row["related_model_signature"])
        if signature:
            by_signature[signature].append(row)
            if related:
                related_signatures[related].add(signature)
                rows_by_related[related].append(row)
            for model_key in signature:
                signatures_by_model[model_key].add(signature)

    for row in rows:
        signature = tuple(row["physical_model_signature"])
        related = tuple(row["related_model_signature"])
        warnings = [
            *row["identity_warnings"],
            *row["scope_classification_warnings"],
            *row["matching_audit"]["warnings"],
        ]
        notes: list[str] = []
        if not signature:
            row["proposed_family_id"] = _stable_id(
                "display-singleton",
                (row["nomenclature_code"], row["product_id"]),
            )
            row["proposal_status"] = "singleton_unresolved_model"
        else:
            exact_group = by_signature[signature]
            if len(exact_group) > 1:
                row["proposed_family_id"] = _stable_id("display-family", signature)
                row["proposal_status"] = "proposed_exact_signature"
            else:
                row["proposed_family_id"] = _stable_id(
                    "display-singleton",
                    (row["nomenclature_code"], row["product_id"]),
                )
                row["proposal_status"] = "singleton_exact_signature"

            overlapping_signatures = set().union(
                *(signatures_by_model[model_key] for model_key in signature)
            )
            if any(other != signature for other in overlapping_signatures):
                warnings.append("partial_model_overlap")
            if related and len(related_signatures[related]) > 1:
                variants = _normalized_variants(rows_by_related[related])
                if variants & _EXPECTED_DEVICE_VARIANTS:
                    notes.append("related_device_variant_separation")
                elif variants and all(_CONNECTIVITY_VARIANT_RE.search(value) for value in variants):
                    warnings.append("connectivity_variant_review")
                else:
                    warnings.append("related_model_identity_review")

        if _has_connectivity_variant(row):
            warnings.append("connectivity_variant_review")

        row["proposal_warnings"] = list(dict.fromkeys(warnings))
        row["proposal_notes"] = list(dict.fromkeys(notes))
        row["requires_manual_review"] = bool(
            row["proposal_status"] == "proposed_exact_signature" or row["proposal_warnings"]
        )


def build_display_family_inventory(
    products: Sequence[Product],
    *,
    evidence_by_code: Mapping[str, DisplayInventoryScopeEvidence] | None,
    matching_evidence_by_product_id: (
        Mapping[int, Sequence[AcceptedCompetitorMatchEvidence]] | None
    ) = None,
    as_of: date,
    history_months: int = 24,
) -> dict[str, Any]:
    cutoff = _subtract_months(as_of, history_months)
    evidence_by_code = evidence_by_code or {}
    matching_evidence_by_product_id = matching_evidence_by_product_id or {}
    scope_result = filter_display_scope_records(products)
    rows: list[dict[str, Any]] = []
    duplicate_codes: Counter[str] = Counter()
    excluded_non_display = 0
    excluded_out_of_scope = 0
    classification_counts: Counter[str] = Counter()
    classification_warning_counts: Counter[str] = Counter()
    classification_conflicts: list[dict[str, Any]] = []

    for product in sorted(
        scope_result.included,
        key=lambda item: (item.id or 0, item.article or ""),
    ):
        classification = classify_display_family_product(product)
        classification_counts[classification.reason] += 1
        classification_warning_counts.update(classification.warnings)
        if classification.warnings:
            classification_conflicts.append(
                {
                    "product_id": product.id,
                    "nomenclature_code": _product_code(product),
                    "name": product.name,
                    "category": product.category,
                    "subject": product.subject,
                    "subject_1c": product.subject_1c,
                    "included": classification.included,
                    "reason": classification.reason,
                    "warnings": list(classification.warnings),
                }
            )
        if not classification.included:
            excluded_non_display += 1
            continue
        code = _product_code(product)
        evidence = evidence_by_code.get(code, DisplayInventoryScopeEvidence())
        reasons = scope_reasons(product, evidence=evidence, cutoff=cutoff)
        if not reasons:
            excluded_out_of_scope += 1
            continue
        identity = display_identity_for_product(product)
        row = _identity_row(
            product=product,
            identity=identity,
            reasons=reasons,
            evidence=evidence,
            classification=classification,
            matching_evidence=matching_evidence_by_product_id.get(product.id, ()),
        )
        duplicate_codes[row["nomenclature_code"]] += 1
        rows.append(row)

    _annotate_family_candidates(rows)
    for row in rows:
        if duplicate_codes[row["nomenclature_code"]] > 1:
            row["proposal_warnings"].append("duplicate_nomenclature_code")
            row["proposal_warnings"] = list(dict.fromkeys(row["proposal_warnings"]))
            row["requires_manual_review"] = True

    rows.sort(key=lambda row: (row["proposed_family_id"], row["segment_id"], row["name"]))
    status_counts = Counter(row["proposal_status"] for row in rows)
    warning_counts = Counter(
        warning for row in rows for warning in row.get("proposal_warnings", ())
    )
    note_counts = Counter(note for row in rows for note in row.get("proposal_notes", ()))
    family_counts = Counter(row["proposed_family_id"] for row in rows)
    matching_relation_counts: Counter[str] = Counter()
    matching_property_counts: Counter[str] = Counter()
    for row in rows:
        matching_relation_counts.update(row["matching_audit"]["relation_counts"])
        matching_property_counts.update(row["matching_audit"]["property_disagreement_counts"])
    payload: dict[str, Any] = {
        "schema": DISPLAY_FAMILY_INVENTORY_SCHEMA,
        "as_of": as_of.isoformat(),
        "history_months": history_months,
        "history_cutoff": cutoff.isoformat(),
        "scope": "active_not_deleted_or_sales_stock_order_within_history_window",
        "summary": {
            "source_product_count": len(products),
            "included_display_sku_count": len(rows),
            "excluded_scope_policy_count": scope_result.audit["excluded_item_count"],
            "excluded_scope_policy_reason_counts": scope_result.audit["excluded_reason_counts"],
            "excluded_non_display_count": excluded_non_display,
            "excluded_display_out_of_scope_count": excluded_out_of_scope,
            "proposed_family_count": len(family_counts),
            "multi_sku_family_count": sum(1 for count in family_counts.values() if count > 1),
            "manual_review_sku_count": sum(1 for row in rows if row["requires_manual_review"]),
            "status_counts": dict(sorted(status_counts.items())),
            "warning_counts": dict(sorted(warning_counts.items())),
            "note_counts": dict(sorted(note_counts.items())),
            "display_scope_reason_counts": dict(sorted(classification_counts.items())),
            "display_scope_warning_counts": dict(sorted(classification_warning_counts.items())),
            "accepted_matching_link_count": sum(
                row["matching_audit"]["accepted_count"] for row in rows
            ),
            "manual_accepted_matching_link_count": sum(
                row["matching_audit"]["manual_accepted_count"] for row in rows
            ),
            "matching_review_sku_count": sum(
                row["matching_audit"]["requires_review"] for row in rows
            ),
            "matching_relation_counts": dict(sorted(matching_relation_counts.items())),
            "matching_property_disagreement_counts": dict(sorted(matching_property_counts.items())),
        },
        "scope_audit": {
            **scope_result.audit,
            "conflict_count": len(classification_conflicts),
            "conflicts": classification_conflicts,
        },
        "items": rows,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    payload["inventory_checksum"] = hashlib.sha256(canonical).hexdigest()
    return payload
