from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.dependencies import get_db, require_management_internal_token
from app.schemas.counterparty_duplicates import (
    CounterpartyDuplicateAckRequest,
    CounterpartyDuplicateAckResponse,
    CounterpartyDuplicateCasePayload,
    CounterpartyDuplicateHealthComponent,
    CounterpartyDuplicateHealthResponse,
    CounterpartyDuplicatePendingResponse,
)
from app.services.counterparty_duplicates import (
    acknowledge_counterparty_duplicate_case,
    build_counterparty_duplicate_health,
    build_counterparty_duplicate_payload,
    get_counterparty_duplicate_case,
    list_pending_counterparty_duplicate_cases,
)

router = APIRouter()


@router.get("/pending", response_model=CounterpartyDuplicatePendingResponse)
def list_pending_counterparty_duplicates(
    db: Session = Depends(get_db),
    _: str = Depends(require_management_internal_token),
):
    items = [
        CounterpartyDuplicateCasePayload.model_validate(build_counterparty_duplicate_payload(item))
        for item in list_pending_counterparty_duplicate_cases(db)
    ]
    return CounterpartyDuplicatePendingResponse(items=items)


@router.post("/{case_id}/ack", response_model=CounterpartyDuplicateAckResponse)
def acknowledge_counterparty_duplicate(
    case_id: int,
    payload: CounterpartyDuplicateAckRequest,
    db: Session = Depends(get_db),
    _: str = Depends(require_management_internal_token),
):
    row = get_counterparty_duplicate_case(db, case_id)
    if row is None:
        raise HTTPException(status_code=404, detail="case not found")
    row = acknowledge_counterparty_duplicate_case(
        db,
        case_id=case_id,
        delivered_at=payload.delivered_at,
        external_case_id=payload.external_case_id,
        external_status=payload.external_status,
        external_url=payload.external_url,
        status=payload.status,
    )
    db.commit()
    db.refresh(row)
    return CounterpartyDuplicateAckResponse(
        case_id=row.id,
        delivery_state=row.delivery_state,
        delivered_at=row.delivered_at,
        external_case_id=row.external_case_id,
        external_status=row.external_status,
        status=row.status,
    )


@router.get("/health", response_model=CounterpartyDuplicateHealthResponse)
def get_counterparty_duplicate_health(
    db: Session = Depends(get_db),
    _: str = Depends(require_management_internal_token),
):
    payload = build_counterparty_duplicate_health(db)
    return CounterpartyDuplicateHealthResponse(
        status=payload["status"],
        freshness_status=payload["freshness_status"],
        source_status=payload["source_status"],
        components=[
            CounterpartyDuplicateHealthComponent.model_validate(item)
            for item in payload["components"]
        ],
    )
