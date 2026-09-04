from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from xml.etree import ElementTree

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import LogisticsManualReview, SiteOrderExecutionCase


@dataclass(frozen=True, slots=True)
class StateProjectionLookup:
    onec_order_number: str | None
    site_order_number: str | None


@dataclass(frozen=True, slots=True)
class StateProjectionRow:
    onec_order_number: str | None
    site_order_number: str | None
    crm_stage: str | None
    logistics_state: str | None
    payment_state: str | None
    debt_amount: Decimal | None
    site_status: str | None
    review_required: bool
    review_reason: str | None
    observed_at: datetime | None
    stale: bool


@dataclass(frozen=True, slots=True)
class CrmProjectionFact:
    site_order_number: str
    bitrix_deal_id: int
    crm_stage: str | None
    delivery_method: str | None
    raw_delivery_method: str | None
    payment_state: str | None
    modified_at: datetime | None
    payment_amount: Decimal | None = None
    debt_amount: Decimal | None = None
    site_status: str | None = None
    site_paid: bool | None = None
    site_canceled: bool | None = None


def upsert_crm_projection(
    session: Session,
    facts: list[CrmProjectionFact],
    *,
    observed_at: datetime,
) -> dict[str, int]:
    grouped: dict[str, list[CrmProjectionFact]] = {}
    for fact in facts:
        order_number = fact.site_order_number.strip()
        if order_number:
            grouped.setdefault(order_number, []).append(fact)
    if not grouped:
        return {"created": 0, "updated": 0, "review": 0}

    existing = {
        case.site_order_number: case
        for case in session.scalars(
            select(SiteOrderExecutionCase).where(
                SiteOrderExecutionCase.site_order_number.in_(grouped)
            )
        ).all()
    }
    created = 0
    updated = 0
    review_count = 0
    for order_number, order_facts in grouped.items():
        ordered = sorted(
            order_facts,
            key=lambda item: (_naive(item.modified_at) or datetime.min, item.bitrix_deal_id),
            reverse=True,
        )
        current = ordered[0]
        case = existing.get(order_number)
        if case is None:
            case = SiteOrderExecutionCase(
                site_order_number=order_number,
                current_derived_status="crm_projection_only",
                payload={},
            )
            session.add(case)
            session.flush()
            existing[order_number] = case
            created += 1
        else:
            updated += 1
        case.bitrix_deal_id = current.bitrix_deal_id if len(ordered) == 1 else None
        case.current_crm_stage = current.crm_stage
        case.delivery_method = current.delivery_method
        case.raw_delivery_method = current.raw_delivery_method
        if current.payment_state:
            case.payment_status = current.payment_state
        current_payload = case.payload if isinstance(case.payload, dict) else {}
        current_state_projection = current_payload.get("state_projection")
        current_state_projection = (
            current_state_projection if isinstance(current_state_projection, dict) else {}
        )
        case.payload = {
            **current_payload,
            "crm_projection": {
                "bitrix_deal_id": current.bitrix_deal_id,
                "crm_stage": current.crm_stage,
                "delivery_method": current.delivery_method,
                "raw_delivery_method": current.raw_delivery_method,
                "payment_state": current.payment_state,
                "payment_amount": _decimal_json(current.payment_amount),
                "debt_amount": _decimal_json(current.debt_amount),
                "site_status": current.site_status,
                "site_paid": current.site_paid,
                "site_canceled": current.site_canceled,
                "modified_at": (
                    current.modified_at.isoformat() if current.modified_at is not None else None
                ),
                "observed_at": observed_at.isoformat(),
                "duplicate_deal_ids": [item.bitrix_deal_id for item in ordered],
            },
            "state_projection": {
                **current_state_projection,
                "payment_amount": _decimal_json(current.payment_amount),
                "debt_amount": _decimal_json(current.debt_amount),
                "site_status": current.site_status,
                "site_paid": current.site_paid,
                "site_canceled": current.site_canceled,
                "observed_at": observed_at.isoformat(),
            },
        }
        case.updated_at = observed_at
        if len(ordered) > 1:
            already_open = session.scalar(
                select(LogisticsManualReview.id).where(
                    LogisticsManualReview.review_type == "site_order_crm_duplicate",
                    LogisticsManualReview.source_external_id == order_number,
                    LogisticsManualReview.status == "open",
                )
            )
            if already_open is None:
                session.add(
                    LogisticsManualReview(
                        review_type="site_order_crm_duplicate",
                        source_document_type="site_order",
                        source_external_id=order_number,
                        reason="multiple_bitrix_deals",
                        payload={"bitrix_deal_ids": [item.bitrix_deal_id for item in ordered]},
                    )
                )
                review_count += 1
    session.flush()
    return {"created": created, "updated": updated, "review": review_count}


def load_state_projection(
    session: Session,
    lookups: list[StateProjectionLookup],
    *,
    stale_after_seconds: int,
    now: datetime | None = None,
) -> list[StateProjectionRow]:
    if len(lookups) > 500:
        raise ValueError("state projection batch is limited to 500 orders")
    if not lookups:
        return []

    site_numbers = {item.site_order_number for item in lookups if item.site_order_number}
    onec_numbers = {item.onec_order_number for item in lookups if item.onec_order_number}
    predicates = []
    if site_numbers:
        predicates.append(SiteOrderExecutionCase.site_order_number.in_(site_numbers))
    if onec_numbers:
        predicates.append(SiteOrderExecutionCase.onec_order_external_id.in_(onec_numbers))
    cases = session.scalars(select(SiteOrderExecutionCase).where(or_(*predicates))).all()

    by_site: dict[str, list[SiteOrderExecutionCase]] = {}
    by_onec: dict[str, list[SiteOrderExecutionCase]] = {}
    for case in cases:
        by_site.setdefault(case.site_order_number, []).append(case)
        if case.onec_order_external_id:
            by_onec.setdefault(case.onec_order_external_id, []).append(case)

    review_site_numbers = {case.site_order_number for case in cases}
    reviews_by_order: dict[str, LogisticsManualReview] = {}
    if review_site_numbers:
        reviews = session.scalars(
            select(LogisticsManualReview)
            .where(
                LogisticsManualReview.status == "open",
                LogisticsManualReview.source_external_id.in_(review_site_numbers),
            )
            .order_by(LogisticsManualReview.created_at.desc(), LogisticsManualReview.id.desc())
        ).all()
        for review in reviews:
            if review.source_external_id:
                reviews_by_order.setdefault(review.source_external_id, review)

    observed_now = now or datetime.now()
    stale_before = observed_now - timedelta(seconds=max(60, stale_after_seconds))
    rows: list[StateProjectionRow] = []
    for lookup in lookups:
        matched: dict[int, SiteOrderExecutionCase] = {}
        for case in by_site.get(lookup.site_order_number or "", []):
            matched[case.id] = case
        for case in by_onec.get(lookup.onec_order_number or "", []):
            matched[case.id] = case
        ordered = sorted(
            matched.values(),
            key=lambda item: (item.updated_at or item.created_at, item.id),
            reverse=True,
        )
        if not ordered:
            rows.append(
                StateProjectionRow(
                    onec_order_number=lookup.onec_order_number,
                    site_order_number=lookup.site_order_number,
                    crm_stage=None,
                    logistics_state=None,
                    payment_state=None,
                    debt_amount=None,
                    site_status=None,
                    review_required=False,
                    review_reason=None,
                    observed_at=None,
                    stale=True,
                )
            )
            continue

        case = ordered[0]
        payload = case.payload if isinstance(case.payload, dict) else {}
        projection = payload.get("state_projection")
        projection = projection if isinstance(projection, dict) else {}
        reconciliation = payload.get("execution_reconciliation")
        reconciliation = reconciliation if isinstance(reconciliation, dict) else {}
        snapshot = reconciliation.get("snapshot")
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        decision = reconciliation.get("decision")
        decision = decision if isinstance(decision, dict) else {}
        review = reviews_by_order.get(case.site_order_number)
        ambiguous = len(ordered) > 1
        decision_review = decision.get("action") == "manual_review"
        review_required = bool(review or ambiguous or decision_review)
        review_reason = (
            "order_identifiers_conflict"
            if ambiguous
            else (
                review.reason
                if review is not None
                else str(decision.get("reason") or "") or None if decision_review else None
            )
        )
        observed_at = case.updated_at or case.created_at
        normalized_observed = _naive(observed_at)
        rows.append(
            StateProjectionRow(
                onec_order_number=case.onec_order_external_id or lookup.onec_order_number,
                site_order_number=case.site_order_number or lookup.site_order_number,
                crm_stage=case.current_crm_stage,
                logistics_state=case.current_derived_status,
                payment_state=case.payment_status,
                debt_amount=_decimal_or_none(
                    projection.get("debt_amount", snapshot.get("debt_amount"))
                ),
                site_status=_string_or_none(
                    projection.get("site_status", snapshot.get("site_status"))
                ),
                review_required=review_required,
                review_reason=review_reason,
                observed_at=observed_at,
                stale=normalized_observed is None or normalized_observed < stale_before,
            )
        )
    return rows


def state_projection_xml(rows: list[StateProjectionRow]) -> bytes:
    root = ElementTree.Element("orders")
    for row in rows:
        node = ElementTree.SubElement(root, "order")
        values: dict[str, Any] = {
            "onec_order_number": row.onec_order_number,
            "site_order_number": row.site_order_number,
            "crm_stage": row.crm_stage,
            "logistics_state": row.logistics_state,
            "payment_state": row.payment_state,
            "debt_amount": row.debt_amount,
            "site_status": row.site_status,
            "review_required": row.review_required,
            "review_reason": row.review_reason,
            "observed_at": row.observed_at,
            "stale": row.stale,
        }
        for key, value in values.items():
            child = ElementTree.SubElement(node, key)
            child.text = _xml_value(value)
    return ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)


def _decimal_or_none(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _decimal_json(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None


def _string_or_none(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _naive(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


def _xml_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)
