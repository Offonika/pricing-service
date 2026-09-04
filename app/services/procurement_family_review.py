from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models.display_family_registry import DisplayFamilyDecisionEvent
from app.models.product import Product
from app.services.procurement_order_formation import VersionConflictError
from app.services.procurement_product_cards import build_product_card_review_snapshot

QUALITY_ACTION = "procurement_quality_review"
DISTRIBUTION_ACTION = "procurement_distribution_review"


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _hash(value: Any) -> str:
    encoded = json.dumps(
        _json_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _facts_snapshot(card: Mapping[str, Any]) -> dict[str, Any]:
    family = dict(card.get("family") or {})
    comparison = list(family.get("comparison_members") or [])
    return {
        "primary_code": dict(card.get("identity") or {}).get("nomenclature_code"),
        "registry_version_id": family.get("registry_version_id"),
        "registry_version_number": family.get("registry_version_number"),
        "registry_inventory_checksum": family.get("registry_inventory_checksum"),
        "family_record_id": family.get("record_id"),
        "family_key": family.get("id"),
        "member_codes": list(family.get("member_codes") or []),
        "comparison_members": [
            {
                "role": item.get("role"),
                "rank": item.get("rank"),
                "speed_score": item.get("speed_score"),
                "card": item.get("card"),
            }
            for item in comparison
            if isinstance(item, Mapping)
        ],
        "source": dict(card.get("source") or {}),
    }


def _event_payload(event: DisplayFamilyDecisionEvent | None) -> dict[str, Any] | None:
    if event is None:
        return None
    evidence = dict(event.evidence_snapshot_json or {})
    return {
        "id": event.id,
        "type": "quality" if event.action == QUALITY_ACTION else "distribution",
        "actor": event.actor,
        "created_at": event.created_at,
        "effective_at": event.effective_at,
        "reason": event.reason,
        "facts_hash": evidence.get("facts_hash"),
        "registry_version_number": evidence.get("registry_version_number"),
        "decision": dict(evidence.get("decision") or {}),
    }


def _current_decisions(
    db: Session,
    *,
    registry_version_id: int | None,
    family_id: int | None,
    facts_hash: str,
) -> dict[str, Any]:
    if registry_version_id is None or family_id is None:
        return {"quality": None, "distribution": None, "blocker_ready": False}
    events = db.scalars(
        select(DisplayFamilyDecisionEvent)
        .where(
            DisplayFamilyDecisionEvent.registry_version_id == registry_version_id,
            DisplayFamilyDecisionEvent.family_id == family_id,
            DisplayFamilyDecisionEvent.action.in_([QUALITY_ACTION, DISTRIBUTION_ACTION]),
        )
        .order_by(
            DisplayFamilyDecisionEvent.created_at.desc(),
            DisplayFamilyDecisionEvent.id.desc(),
        )
    ).all()
    current: dict[str, DisplayFamilyDecisionEvent] = {}
    for event in events:
        evidence = dict(event.evidence_snapshot_json or {})
        if evidence.get("facts_hash") != facts_hash or event.action in current:
            continue
        current[event.action] = event
    quality = _event_payload(current.get(QUALITY_ACTION))
    distribution = _event_payload(current.get(DISTRIBUTION_ACTION))
    quality_resolved = bool(
        quality
        and dict(quality.get("decision") or {}).get("result") in {"confirmed", "false_positive"}
    )
    return {
        "quality": quality,
        "distribution": distribution,
        "blocker_ready": quality_resolved and distribution is not None,
    }


def build_family_review_card(
    db: Session,
    *,
    nomenclature_code: str,
    settings: Settings | None = None,
) -> dict[str, Any]:
    card = build_product_card_review_snapshot(
        db,
        nomenclature_code=nomenclature_code,
        settings=settings,
    )
    facts = _json_value(_facts_snapshot(card))
    facts_hash = _hash(facts)
    family = dict(card.get("family") or {})
    card["facts_hash"] = facts_hash
    card["facts_snapshot"] = facts
    card["review_requirements"] = {"quality": True, "distribution": True}
    card["decisions"] = _current_decisions(
        db,
        registry_version_id=family.get("registry_version_id"),
        family_id=family.get("record_id"),
        facts_hash=facts_hash,
    )
    return card


def save_family_review_decision(
    db: Session,
    *,
    nomenclature_code: str,
    kind: str,
    expected_facts_hash: str,
    expected_registry_version_number: int,
    decision: Mapping[str, Any],
    actor: str,
    settings: Settings | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    if not settings.procurement_family_review_decisions_enabled:
        raise PermissionError("сохранение решений временно выключено")
    if kind not in {"quality", "distribution"}:
        raise ValueError("unknown family review decision kind")
    card = build_family_review_card(
        db,
        nomenclature_code=nomenclature_code,
        settings=settings,
    )
    family = dict(card.get("family") or {})
    if (
        card["facts_hash"] != expected_facts_hash
        or family.get("registry_version_number") != expected_registry_version_number
    ):
        raise VersionConflictError(
            "исходные данные изменились; обновите разбор и проверьте решение повторно"
        )
    family_id = family.get("record_id")
    registry_version_id = family.get("registry_version_id")
    if not family_id or not registry_version_id:
        raise ValueError("товар не найден в активном семейном реестре")

    if kind == "distribution":
        expected_codes = set(family.get("member_codes") or [])
        actual_codes = set(dict(decision.get("quantities") or {}))
        if actual_codes != expected_codes:
            missing = sorted(expected_codes - actual_codes)
            extra = sorted(actual_codes - expected_codes)
            details = []
            if missing:
                details.append("не указаны: " + ", ".join(missing))
            if extra:
                details.append("лишние: " + ", ".join(extra))
            raise ValueError(
                "нужно указать количество каждого члена семьи, включая нули"
                + (" (" + "; ".join(details) + ")" if details else "")
            )

    normalized = _json_value(dict(decision))
    action = QUALITY_ACTION if kind == "quality" else DISTRIBUTION_ACTION
    request_hash = _hash(
        {
            "action": action,
            "actor": actor,
            "facts_hash": card["facts_hash"],
            "decision": normalized,
        }
    )
    existing = db.scalar(
        select(DisplayFamilyDecisionEvent)
        .where(
            DisplayFamilyDecisionEvent.registry_version_id == registry_version_id,
            DisplayFamilyDecisionEvent.family_id == family_id,
            DisplayFamilyDecisionEvent.action == action,
        )
        .order_by(
            DisplayFamilyDecisionEvent.created_at.desc(),
            DisplayFamilyDecisionEvent.id.desc(),
        )
    )
    if (
        existing is not None
        and dict(existing.evidence_snapshot_json or {}).get("request_hash") == request_hash
    ):
        event = existing
        idempotent = True
    else:
        primary_product_id = db.scalar(
            select(Product.id).where(Product.code_1c == nomenclature_code)
        )
        comment = str(normalized.get("comment") or "").strip()
        reason = str(
            normalized.get("root_cause")
            or normalized.get("rationale")
            or comment
            or "Решение по семейному разбору"
        )[:500]
        event = DisplayFamilyDecisionEvent(
            registry_version_id=int(registry_version_id),
            family_id=int(family_id),
            product_id=int(primary_product_id) if primary_product_id else None,
            action=action,
            actor=str(actor or "unknown")[:160],
            reason=reason,
            effective_at=date.today(),
            evidence_snapshot_json={
                "schema": "procurement_family_review_decision.v1",
                "kind": kind,
                "request_hash": request_hash,
                "facts_hash": card["facts_hash"],
                "registry_version_number": family.get("registry_version_number"),
                "registry_inventory_checksum": family.get("registry_inventory_checksum"),
                "nomenclature_code": nomenclature_code,
                "decision": normalized,
                "facts_snapshot": card["facts_snapshot"],
            },
        )
        db.add(event)
        db.flush()
        idempotent = False

    refreshed = build_family_review_card(
        db,
        nomenclature_code=nomenclature_code,
        settings=settings,
    )
    return {
        "event": _event_payload(event),
        "idempotent": idempotent,
        "decisions": refreshed["decisions"],
        "blocker_ready": refreshed["decisions"]["blocker_ready"],
    }
