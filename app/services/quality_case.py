from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.quality_case import QualityCase, QualityCaseEvent

STATUS_PENDING_REVIEW = "pending_review"
STATUS_UNDER_REVIEW = "under_review"
STATUS_DECIDED = "decided"
STATUS_CLOSED = "closed"

PRODUCT_DEFECT_DECISIONS = {"factory_defect", "supplier_defect", "technical_defect"}
HANDLING_DAMAGE_DECISIONS = {"transport_damage", "internal_handling_damage"}


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _serialize(row: QualityCase) -> dict[str, Any]:
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


def _serialize_event(row: QualityCaseEvent) -> dict[str, Any]:
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


def _get_case(session: Session, case_id: int) -> QualityCase:
    row = session.get(QualityCase, case_id)
    if row is None:
        raise HTTPException(status_code=404, detail="quality case not found")
    return row


def _existing_event(session: Session, idempotency_key: str | None) -> QualityCaseEvent | None:
    if not idempotency_key:
        return None
    return session.scalar(
        select(QualityCaseEvent).where(QualityCaseEvent.idempotency_key == idempotency_key)
    )


def _append_event(
    session: Session,
    *,
    row: QualityCase,
    event_type: str,
    actor_external_id: str | None,
    source: str,
    comment: str | None,
    idempotency_key: str | None,
    meta: dict[str, Any] | None = None,
) -> None:
    session.add(
        QualityCaseEvent(
            quality_case_id=row.id,
            event_type=event_type,
            event_at=_now(),
            actor_external_id=actor_external_id,
            source=source,
            comment=comment,
            idempotency_key=idempotency_key,
            meta=meta,
        )
    )


def sync_case(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    row = session.scalar(
        select(QualityCase).where(QualityCase.external_id == payload["external_id"])
    )
    created = row is None
    if created:
        row = QualityCase(
            external_id=payload["external_id"],
            source_return_ref=payload["source_return_ref"],
            source_return_number=payload.get("source_return_number"),
            source_return_line_key=payload["source_return_line_key"],
            return_at=payload["return_at"],
            nomenclature_ref=payload.get("nomenclature_ref"),
            nomenclature_code=payload["nomenclature_code"],
            nomenclature_name=payload.get("nomenclature_name"),
            quantity=payload["quantity"],
            store_external_id=payload.get("store_external_id"),
            store_name=payload.get("store_name"),
            preliminary_quality=payload.get("preliminary_quality"),
            preliminary_reason_code=payload.get("preliminary_reason_code"),
            current_status=STATUS_PENDING_REVIEW,
            owner_external_id=payload.get("owner_external_id"),
            due_at=payload.get("due_at"),
            payload=payload.get("payload"),
        )
        session.add(row)
        session.flush()
        _append_event(
            session,
            row=row,
            event_type="case_created",
            actor_external_id=None,
            source="sync",
            comment=None,
            idempotency_key=payload.get("idempotency_key"),
        )
    else:
        # Повторный read-only sync обновляет только исходные факты. Итог ОКК и
        # статус не могут быть затёрты предварительным качеством продавца.
        for key in (
            "source_return_number",
            "return_at",
            "nomenclature_ref",
            "nomenclature_code",
            "nomenclature_name",
            "quantity",
            "store_external_id",
            "store_name",
            "preliminary_quality",
            "preliminary_reason_code",
            "owner_external_id",
            "due_at",
            "payload",
        ):
            if key in payload:
                setattr(row, key, payload[key])
    session.commit()
    session.refresh(row)
    return _serialize(row)


def list_cases(
    session: Session,
    *,
    status: str | None = None,
    nomenclature_code: str | None = None,
) -> list[dict[str, Any]]:
    stmt = select(QualityCase).order_by(QualityCase.return_at.desc(), QualityCase.id.desc())
    if status:
        stmt = stmt.where(QualityCase.current_status == status)
    if nomenclature_code:
        stmt = stmt.where(QualityCase.nomenclature_code == nomenclature_code)
    return [_serialize(row) for row in session.scalars(stmt).all()]


def get_case(session: Session, case_id: int) -> dict[str, Any]:
    return _serialize(_get_case(session, case_id))


def get_history(session: Session, case_id: int) -> list[dict[str, Any]]:
    _get_case(session, case_id)
    rows = session.scalars(
        select(QualityCaseEvent)
        .where(QualityCaseEvent.quality_case_id == case_id)
        .order_by(QualityCaseEvent.event_at.desc(), QualityCaseEvent.id.desc())
    ).all()
    return [_serialize_event(row) for row in rows]


def start_review(
    session: Session,
    *,
    case_id: int,
    actor_external_id: str,
    comment: str | None,
    idempotency_key: str | None,
) -> dict[str, Any]:
    row = _get_case(session, case_id)
    if _existing_event(session, idempotency_key):
        return _serialize(row)
    if row.current_status != STATUS_PENDING_REVIEW:
        raise HTTPException(status_code=409, detail="quality case is not pending review")
    row.current_status = STATUS_UNDER_REVIEW
    _append_event(
        session,
        row=row,
        event_type="review_started",
        actor_external_id=actor_external_id,
        source="api",
        comment=comment,
        idempotency_key=idempotency_key,
    )
    session.commit()
    session.refresh(row)
    return _serialize(row)


def record_decision(
    session: Session,
    *,
    case_id: int,
    actor_external_id: str,
    decision_code: str,
    disposition_code: str,
    onec_quality_correction_ref: str | None,
    comment: str | None,
    idempotency_key: str | None,
) -> dict[str, Any]:
    row = _get_case(session, case_id)
    if _existing_event(session, idempotency_key):
        return _serialize(row)
    if row.current_status not in {STATUS_PENDING_REVIEW, STATUS_UNDER_REVIEW}:
        raise HTTPException(status_code=409, detail="quality case already decided")
    if (
        disposition_code in {"return_to_stock", "repair_then_return_to_stock"}
        and not onec_quality_correction_ref
    ):
        raise HTTPException(
            status_code=422,
            detail="onec_quality_correction_ref is required when goods return to stock",
        )
    decided_at = _now()
    row.current_status = STATUS_DECIDED
    row.final_decision_code = decision_code
    row.disposition_code = disposition_code
    row.decision_comment = comment
    row.decision_author_external_id = actor_external_id
    row.decided_at = decided_at
    row.onec_quality_correction_ref = onec_quality_correction_ref
    row.counts_as_confirmed_product_defect = decision_code in PRODUCT_DEFECT_DECISIONS
    _append_event(
        session,
        row=row,
        event_type="decision_recorded",
        actor_external_id=actor_external_id,
        source="api",
        comment=comment,
        idempotency_key=idempotency_key,
        meta={
            "decision_code": decision_code,
            "disposition_code": disposition_code,
            "counts_as_confirmed_product_defect": row.counts_as_confirmed_product_defect,
            "onec_quality_correction_ref": onec_quality_correction_ref,
        },
    )
    session.commit()
    session.refresh(row)
    return _serialize(row)


def quality_metrics(
    session: Session, *, date_from: datetime, date_to: datetime
) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(QualityCase).where(
            QualityCase.return_at >= date_from,
            QualityCase.return_at < date_to,
        )
    ).all()
    grouped: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: {
            "candidate_qty": Decimal("0"),
            "pending_qty": Decimal("0"),
            "confirmed_product_defect_qty": Decimal("0"),
            "handling_damage_qty": Decimal("0"),
            "confirmed_not_product_defect_qty": Decimal("0"),
        }
    )
    for row in rows:
        bucket = grouped[row.nomenclature_code]
        qty = Decimal(row.quantity)
        bucket["candidate_qty"] += qty
        if row.current_status in {STATUS_PENDING_REVIEW, STATUS_UNDER_REVIEW}:
            bucket["pending_qty"] += qty
        elif row.counts_as_confirmed_product_defect:
            bucket["confirmed_product_defect_qty"] += qty
        elif row.final_decision_code in HANDLING_DAMAGE_DECISIONS:
            bucket["handling_damage_qty"] += qty
        else:
            bucket["confirmed_not_product_defect_qty"] += qty
    return [{"nomenclature_code": code, **values} for code, values in sorted(grouped.items())]
