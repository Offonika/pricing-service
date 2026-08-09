"""Versioned supplier profiles for the procurement order assistant."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from statistics import median
from typing import Any, Iterable, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models.procurement_order_formation import (
    ProcurementOrderFormation,
    ProcurementOrderFormationEvent,
    ProcurementSupplierProfile,
)
from app.services.bitrix_procurement_order_formation_auth import (
    ProcurementOrderFormationSession,
)
from app.services.procurement_order_formation import (
    VersionConflictError,
    ensure_classification_approver,
)

SUPPLIER_CLASS_LABELS = {
    "A": "Лучшие условия и надёжность",
    "B": "Стандартные рабочие условия",
    "C": "Предоплата или повышенный риск",
}


def serialize_supplier_profile(profile: ProcurementSupplierProfile | None) -> dict[str, Any]:
    if profile is None:
        return empty_supplier_profile()
    populated_facts = [
        profile.history_order_count,
        profile.supplier_prepare_days,
        profile.logistics_days,
        profile.lead_time_days,
        profile.price_history_count,
        profile.supplier_defect_pct,
    ]
    has_manual = bool(
        profile.qualification_class
        or profile.qualification_label
        or profile.advantages
        or profile.internal_note
    )
    has_terms = profile.terms_status in {"ready", "partial"}
    populated = sum(value is not None for value in populated_facts)
    data_status = "ready" if has_manual and has_terms and populated >= 3 else "partial"
    if not has_manual and not has_terms and not populated:
        data_status = "missing"
    return {
        "supplier_ref": profile.supplier_ref,
        "supplier_code": profile.supplier_code,
        "supplier_name": profile.supplier_name,
        "version": profile.version,
        "qualification_class": profile.qualification_class,
        "qualification_label": profile.qualification_label,
        "class_description": SUPPLIER_CLASS_LABELS.get(
            str(profile.qualification_class or "").upper()
        ),
        "profitability_pct": _decimal_or_none(profile.facts_payload.get("profitability_pct")),
        "defect_pct": profile.supplier_defect_pct,
        "defect_history_units": profile.supplier_defect_history_units,
        "defect_confidence": profile.supplier_defect_confidence,
        "defect_attribution": (
            "supplier_exact" if profile.supplier_defect_pct is not None else "unconfirmed"
        ),
        "on_time_pct": _decimal_or_none(profile.facts_payload.get("on_time_pct")),
        "payment_terms": profile.payment_terms,
        "credit_days": profile.credit_days,
        "credit_limit": profile.credit_limit,
        "terms_source": profile.terms_source,
        "terms_status": profile.terms_status,
        "advantages": list(profile.advantages or []),
        "internal_note": profile.internal_note,
        "history_order_count": profile.history_order_count,
        "supplier_prepare_days": profile.supplier_prepare_days,
        "logistics_days": profile.logistics_days,
        "lead_time_days": profile.lead_time_days,
        "lead_time_confidence": profile.lead_time_confidence,
        "price_history_count": profile.price_history_count,
        "facts_updated_at": profile.facts_updated_at,
        "manual_updated_at": profile.manual_updated_at,
        "manual_updated_by_name": profile.manual_updated_by_name,
        "updated_at": profile.updated_at,
        "data_status": data_status,
        "can_edit": False,
    }


def empty_supplier_profile(
    *,
    supplier_ref: str | None = None,
    supplier_code: str | None = None,
    supplier_name: str | None = None,
) -> dict[str, Any]:
    return {
        "supplier_ref": supplier_ref,
        "supplier_code": supplier_code,
        "supplier_name": supplier_name,
        "version": 0,
        "qualification_class": None,
        "qualification_label": None,
        "class_description": None,
        "profitability_pct": None,
        "defect_pct": None,
        "defect_history_units": None,
        "defect_confidence": None,
        "defect_attribution": "unconfirmed",
        "on_time_pct": None,
        "payment_terms": None,
        "credit_days": None,
        "credit_limit": None,
        "terms_source": "onec_contract",
        "terms_status": "missing",
        "advantages": [],
        "internal_note": None,
        "history_order_count": None,
        "supplier_prepare_days": None,
        "logistics_days": None,
        "lead_time_days": None,
        "lead_time_confidence": None,
        "price_history_count": None,
        "facts_updated_at": None,
        "manual_updated_at": None,
        "manual_updated_by_name": None,
        "updated_at": None,
        "data_status": "missing",
        "can_edit": False,
    }


def supplier_profiles_by_ref(
    db: Session,
    supplier_refs: Iterable[str | None],
) -> dict[str, ProcurementSupplierProfile]:
    refs = sorted({_normalize_ref(value) for value in supplier_refs if _normalize_ref(value)})
    if not refs:
        return {}
    rows = db.scalars(
        select(ProcurementSupplierProfile).where(ProcurementSupplierProfile.supplier_ref.in_(refs))
    ).all()
    return {row.supplier_ref: row for row in rows}


def get_supplier_profile(db: Session, supplier_ref: str) -> ProcurementSupplierProfile | None:
    return db.scalar(
        select(ProcurementSupplierProfile).where(
            ProcurementSupplierProfile.supplier_ref == _normalize_ref(supplier_ref)
        )
    )


def update_supplier_profile(
    db: Session,
    *,
    supplier_ref: str,
    values: Mapping[str, Any],
    session: ProcurementOrderFormationSession,
    settings: Settings | None = None,
) -> ProcurementSupplierProfile:
    ensure_classification_approver(session.user_id, settings=settings or get_settings())
    normalized_ref = _normalize_ref(supplier_ref)
    if not normalized_ref:
        raise ValueError("supplier_ref is required")
    expected_version = int(values.get("expected_version") or 0)
    profile = db.scalar(
        select(ProcurementSupplierProfile)
        .where(ProcurementSupplierProfile.supplier_ref == normalized_ref)
        .with_for_update()
    )
    if profile is None:
        if expected_version != 0:
            raise VersionConflictError("supplier profile version conflict")
        supplier = db.scalar(
            select(ProcurementOrderFormation)
            .where(ProcurementOrderFormation.supplier_ref == normalized_ref)
            .order_by(ProcurementOrderFormation.updated_at.desc())
        )
        if supplier is None:
            raise LookupError("supplier was not found in procurement orders")
        profile = ProcurementSupplierProfile(
            supplier_ref=normalized_ref,
            supplier_code=supplier.supplier_code,
            supplier_name=supplier.supplier_name,
            version=1,
            advantages=[],
            terms_source="onec_contract",
            terms_status="missing",
            facts_payload={},
        )
        before = empty_supplier_profile(
            supplier_ref=normalized_ref,
            supplier_code=supplier.supplier_code,
            supplier_name=supplier.supplier_name,
        )
        db.add(profile)
        db.flush()
    else:
        if profile.version != expected_version:
            raise VersionConflictError("supplier profile version conflict")
        before = serialize_supplier_profile(profile)
        profile.version += 1

    qualification_class = str(values.get("qualification_class") or "").strip().upper()
    if qualification_class and qualification_class not in SUPPLIER_CLASS_LABELS:
        raise ValueError("supplier class must be A, B or C")
    advantages = _advantages(values.get("advantages"))
    profile.qualification_class = qualification_class or None
    profile.qualification_label = _optional_text(values.get("qualification_label"), 255)
    profile.advantages = advantages
    profile.internal_note = _optional_text(values.get("internal_note"), 4000)
    now = datetime.now(UTC).replace(tzinfo=None)
    profile.manual_updated_by_actor = session.actor
    profile.manual_updated_by_bitrix_user_id = session.user_id
    profile.manual_updated_by_name = session.user_name or session.actor
    profile.manual_updated_at = now
    db.flush()
    after = serialize_supplier_profile(profile)
    db.add(
        ProcurementOrderFormationEvent(
            order_id=None,
            entity_type="supplier_profile",
            entity_id=normalized_ref,
            event_type="supplier_profile_updated",
            actor=session.actor,
            bitrix_user_id=session.user_id,
            user_name=session.user_name,
            before=_jsonable(before),
            after=_jsonable(after),
            payload={"source": "manual", "onec_write": False, "bitrix_write": False},
        )
    )
    return profile


def upsert_supplier_profile_facts(
    db: Session,
    *,
    supplier_ref: str,
    supplier_code: str | None,
    supplier_name: str,
    facts: Mapping[str, Any],
    run_id: str,
) -> tuple[ProcurementSupplierProfile, bool]:
    normalized_ref = _normalize_ref(supplier_ref)
    if not normalized_ref:
        raise ValueError("supplier_ref is required")
    profile = db.scalar(
        select(ProcurementSupplierProfile)
        .where(ProcurementSupplierProfile.supplier_ref == normalized_ref)
        .with_for_update()
    )
    is_new = profile is None
    if profile is None:
        profile = ProcurementSupplierProfile(
            supplier_ref=normalized_ref,
            supplier_code=supplier_code,
            supplier_name=supplier_name,
            version=1,
            advantages=[],
            terms_source="onec_contract",
            terms_status="missing",
            facts_payload={},
        )
        db.add(profile)
        db.flush()
    before = serialize_supplier_profile(profile)
    next_values = _normalized_fact_values(facts)
    current_values = _profile_fact_values(profile)
    changed = current_values != next_values or profile.supplier_name != supplier_name
    if not changed:
        return profile, False
    profile.supplier_code = supplier_code or profile.supplier_code
    profile.supplier_name = supplier_name
    profile.history_order_count = next_values["history_order_count"]
    profile.supplier_prepare_days = next_values["supplier_prepare_days"]
    profile.logistics_days = next_values["logistics_days"]
    profile.lead_time_days = next_values["lead_time_days"]
    profile.lead_time_confidence = next_values["lead_time_confidence"]
    profile.price_history_count = next_values["price_history_count"]
    profile.supplier_defect_pct = next_values["supplier_defect_pct"]
    profile.supplier_defect_history_units = next_values["supplier_defect_history_units"]
    profile.supplier_defect_confidence = next_values["supplier_defect_confidence"]
    profile.payment_terms = next_values["payment_terms"]
    profile.credit_days = next_values["credit_days"]
    profile.credit_limit = next_values["credit_limit"]
    profile.terms_source = next_values["terms_source"]
    profile.terms_status = next_values["terms_status"]
    profile.facts_payload = dict(next_values["facts_payload"])
    profile.facts_updated_at = datetime.now(UTC).replace(tzinfo=None)
    if not is_new:
        profile.version += 1
    db.flush()
    db.add(
        ProcurementOrderFormationEvent(
            order_id=None,
            entity_type="supplier_profile",
            entity_id=normalized_ref,
            event_type="supplier_profile_facts_refreshed",
            actor="system:procurement-order-metrics",
            idempotency_key=f"{run_id}:supplier:{normalized_ref}",
            before=_jsonable(before),
            after=_jsonable(serialize_supplier_profile(profile)),
            payload={"source": "onec_read_only", "onec_write": False, "bitrix_write": False},
        )
    )
    return profile, True


def aggregate_supplier_facts(lines: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    rows = [dict(row) for row in lines]
    prepare = _integer_values(rows, "supplier_prepare_days")
    logistics = _integer_values(rows, "logistics_days")
    lead = _integer_values(rows, "lead_time_days")
    price_counts = _integer_values(rows, "price_history_count")
    confidences = [str(row.get("lead_time_confidence") or "") for row in rows]
    confidence = _lowest_confidence(confidences)
    return {
        "history_order_count": max(_integer_values(rows, "supplier_history_order_count") or [0])
        or None,
        "supplier_prepare_days": _median_int(prepare),
        "logistics_days": _median_int(logistics),
        "lead_time_days": _median_int(lead),
        "lead_time_confidence": confidence,
        "price_history_count": max(price_counts or [0]) or None,
        "supplier_defect_pct": _first_value(rows, "supplier_defect_pct"),
        "supplier_defect_history_units": _first_value(rows, "supplier_defect_history_units"),
        "supplier_defect_confidence": _first_value(rows, "supplier_defect_confidence"),
        "payment_terms": None,
        "credit_days": None,
        "credit_limit": None,
        "terms_source": "onec_contract",
        "terms_status": "missing",
        "facts_payload": {
            "lead_time_source": "onec_supplier_order_history",
            "supplier_defect_attribution": (
                "supplier_exact"
                if _first_value(rows, "supplier_defect_pct") is not None
                else "unconfirmed"
            ),
        },
    }


def _normalized_fact_values(facts: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "history_order_count": _int_or_none(facts.get("history_order_count")),
        "supplier_prepare_days": _int_or_none(facts.get("supplier_prepare_days")),
        "logistics_days": _int_or_none(facts.get("logistics_days")),
        "lead_time_days": _int_or_none(facts.get("lead_time_days")),
        "lead_time_confidence": _optional_text(facts.get("lead_time_confidence"), 32),
        "price_history_count": _int_or_none(facts.get("price_history_count")),
        "supplier_defect_pct": _decimal_or_none(facts.get("supplier_defect_pct")),
        "supplier_defect_history_units": _int_or_none(facts.get("supplier_defect_history_units")),
        "supplier_defect_confidence": _optional_text(facts.get("supplier_defect_confidence"), 32),
        "payment_terms": _optional_text(facts.get("payment_terms"), 500),
        "credit_days": _int_or_none(facts.get("credit_days")),
        "credit_limit": _decimal_or_none(facts.get("credit_limit")),
        "terms_source": _optional_text(facts.get("terms_source"), 64) or "onec_contract",
        "terms_status": (
            str(facts.get("terms_status") or "missing")
            if str(facts.get("terms_status") or "missing") in {"ready", "partial", "missing"}
            else "missing"
        ),
        "facts_payload": dict(facts.get("facts_payload") or {}),
    }


def _profile_fact_values(profile: ProcurementSupplierProfile) -> dict[str, Any]:
    return {
        "history_order_count": profile.history_order_count,
        "supplier_prepare_days": profile.supplier_prepare_days,
        "logistics_days": profile.logistics_days,
        "lead_time_days": profile.lead_time_days,
        "lead_time_confidence": profile.lead_time_confidence,
        "price_history_count": profile.price_history_count,
        "supplier_defect_pct": profile.supplier_defect_pct,
        "supplier_defect_history_units": profile.supplier_defect_history_units,
        "supplier_defect_confidence": profile.supplier_defect_confidence,
        "payment_terms": profile.payment_terms,
        "credit_days": profile.credit_days,
        "credit_limit": profile.credit_limit,
        "terms_source": profile.terms_source,
        "terms_status": profile.terms_status,
        "facts_payload": dict(profile.facts_payload or {}),
    }


def _advantages(value: Any) -> list[str]:
    raw = value if isinstance(value, list) else str(value or "").split(";")
    result: list[str] = []
    for item in raw:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text[:255])
        if len(result) >= 20:
            break
    return result


def _normalize_ref(value: Any) -> str:
    return str(value or "").strip().lower()


def _optional_text(value: Any, max_length: int) -> str | None:
    text = str(value or "").strip()
    return text[:max_length] if text else None


def _decimal_or_none(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(Decimal(str(value)))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _integer_values(rows: Iterable[Mapping[str, Any]], key: str) -> list[int]:
    return [value for row in rows if (value := _int_or_none(row.get(key))) is not None]


def _median_int(values: list[int]) -> int | None:
    return round(median(values)) if values else None


def _lowest_confidence(values: Iterable[str]) -> str | None:
    ranks = {"high": 3, "medium": 2, "low": 1}
    present = [value for value in values if value in ranks]
    return min(present, key=lambda value: ranks[value]) if present else None


def _first_value(rows: Iterable[Mapping[str, Any]], key: str) -> Any:
    return next((row.get(key) for row in rows if row.get(key) not in (None, "")), None)


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value
