from collections import Counter
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_db
from app.models.procurement_exception import ProcurementException
from app.schemas.procurement_exceptions import (
    ProcurementControlSummary,
    ProcurementExceptionDecision,
    ProcurementExceptionList,
    ProcurementExceptionRead,
)
from app.services.bitrix_procurement_order_formation_auth import (
    ProcurementOrderFormationSession,
    verify_procurement_order_formation_session,
)
from app.services.procurement_exceptions import (
    control_summary,
    decide_exception,
    serialize_exception,
)
from app.services.procurement_order_formation import VersionConflictError

router = APIRouter()


@router.get("/exceptions", response_model=ProcurementExceptionList)
def exceptions(
    status: str = "open",
    reason: str = "",
    overdue_only: bool = False,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    _session: ProcurementOrderFormationSession = Depends(
        verify_procurement_order_formation_session
    ),
):
    statement = select(ProcurementException).order_by(
        ProcurementException.response_due_at, ProcurementException.id
    )
    if status == "open":
        statement = statement.where(ProcurementException.status != "resolved")
    elif status != "all":
        statement = statement.where(ProcurementException.status == status)
    if reason:
        statement = statement.where(ProcurementException.reason_code == reason)
    now = datetime.now(UTC)
    items = [serialize_exception(item, now=now) for item in db.scalars(statement)]
    if overdue_only:
        items = [item for item in items if item["overdue"]]
    return {
        "total": len(items),
        "offset": offset,
        "limit": limit,
        "items": items[offset : offset + limit],
        "by_reason": dict(Counter(item["reason_code"] for item in items)),
        "overdue_count": sum(item["overdue"] for item in items),
    }


@router.post("/exceptions/{exception_id}/decision", response_model=ProcurementExceptionRead)
def decision(
    exception_id: int,
    payload: ProcurementExceptionDecision,
    db: Session = Depends(get_db),
    session: ProcurementOrderFormationSession = Depends(verify_procurement_order_formation_session),
):
    try:
        item = decide_exception(
            db,
            exception_id,
            values=payload.model_dump(),
            user_id=session.user_id,
            actor=session.actor,
        )
        result = serialize_exception(item)
        db.commit()
        return result
    except VersionConflictError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    except (ValueError, TypeError) as exc:
        db.rollback()
        raise HTTPException(422, str(exc)) from exc


@router.get("/control-summary", response_model=ProcurementControlSummary)
def summary(
    db: Session = Depends(get_db),
    _session: ProcurementOrderFormationSession = Depends(
        verify_procurement_order_formation_session
    ),
):
    return control_summary(db)
