from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.dependencies import get_db, require_expertise_internal_token
from app.schemas.expertise import (
    ExpertiseCaseActionRequest,
    ExpertiseCaseCompletionRequest,
    ExpertiseCaseDecisionRequest,
    ExpertiseCaseDetailResponse,
    ExpertiseCaseEventResponse,
    ExpertiseCaseListItem,
    ExpertiseCaseSyncItem,
    ExpertiseSyncResponse,
)
from app.services import expertise as expertise_service

router = APIRouter(dependencies=[Depends(require_expertise_internal_token)])


@router.post("/sync/cases", response_model=ExpertiseSyncResponse)
def sync_cases(items: list[ExpertiseCaseSyncItem], db: Session = Depends(get_db)):
    return expertise_service.sync_cases(db, [item.model_dump(exclude_unset=True) for item in items])


@router.get("/cases", response_model=list[ExpertiseCaseListItem])
def list_cases(
    status: str | None = Query(default=None),
    store_external_id: str | None = Query(default=None),
    owner_user_external_id: str | None = Query(default=None),
    overdue: bool | None = Query(default=None),
    client_notified: bool | None = Query(default=None),
    db: Session = Depends(get_db),
):
    return expertise_service.list_cases(
        db,
        status=status,
        store_external_id=store_external_id,
        owner_user_external_id=owner_user_external_id,
        overdue=overdue,
        client_notified=client_notified,
    )


@router.get("/cases/{case_id}", response_model=ExpertiseCaseDetailResponse)
def get_case(case_id: int, db: Session = Depends(get_db)):
    return expertise_service.get_case(db, case_id=case_id)


@router.get("/cases/{case_id}/history", response_model=list[ExpertiseCaseEventResponse])
def get_case_history(case_id: int, db: Session = Depends(get_db)):
    return expertise_service.get_case_history(db, case_id=case_id)


@router.post("/cases/{case_id}/receive", response_model=ExpertiseCaseDetailResponse)
def receive_case(
    case_id: int,
    payload: ExpertiseCaseActionRequest,
    db: Session = Depends(get_db),
):
    return expertise_service.receive_case(
        db,
        case_id=case_id,
        actor_external_id=payload.actor_external_id,
        comment=payload.comment,
        idempotency_key=payload.idempotency_key,
    )


@router.post("/cases/{case_id}/start-review", response_model=ExpertiseCaseDetailResponse)
def start_review(
    case_id: int,
    payload: ExpertiseCaseActionRequest,
    db: Session = Depends(get_db),
):
    return expertise_service.start_review(
        db,
        case_id=case_id,
        actor_external_id=payload.actor_external_id,
        comment=payload.comment,
        idempotency_key=payload.idempotency_key,
    )


@router.post("/cases/{case_id}/decision", response_model=ExpertiseCaseDetailResponse)
def record_decision(
    case_id: int,
    payload: ExpertiseCaseDecisionRequest,
    db: Session = Depends(get_db),
):
    return expertise_service.record_decision(
        db,
        case_id=case_id,
        actor_external_id=payload.actor_external_id,
        decision_code=payload.decision_code,
        decision_comment=payload.decision_comment,
        comment=payload.comment,
        idempotency_key=payload.idempotency_key,
    )


@router.post("/cases/{case_id}/client-notified", response_model=ExpertiseCaseDetailResponse)
def mark_client_notified(
    case_id: int,
    payload: ExpertiseCaseActionRequest,
    db: Session = Depends(get_db),
):
    return expertise_service.mark_client_notified(
        db,
        case_id=case_id,
        actor_external_id=payload.actor_external_id,
        comment=payload.comment,
        idempotency_key=payload.idempotency_key,
    )


@router.post("/cases/{case_id}/complete", response_model=ExpertiseCaseDetailResponse)
def complete_case(
    case_id: int,
    payload: ExpertiseCaseCompletionRequest,
    db: Session = Depends(get_db),
):
    return expertise_service.complete_case(
        db,
        case_id=case_id,
        actor_external_id=payload.actor_external_id,
        completion_outcome=payload.completion_outcome,
        comment=payload.comment,
        idempotency_key=payload.idempotency_key,
    )


@router.post("/cases/{case_id}/return-to-store", response_model=ExpertiseCaseDetailResponse)
def return_to_store(
    case_id: int,
    payload: ExpertiseCaseActionRequest,
    db: Session = Depends(get_db),
):
    return expertise_service.complete_case(
        db,
        case_id=case_id,
        actor_external_id=payload.actor_external_id,
        completion_outcome="returned_to_store",
        comment=payload.comment,
        idempotency_key=payload.idempotency_key,
    )
