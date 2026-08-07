from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.dependencies import get_db, require_expertise_internal_token
from app.schemas.quality_case import (
    QualityCaseActionRequest,
    QualityCaseDecisionRequest,
    QualityCaseEventResponse,
    QualityCaseResponse,
    QualityCaseSyncItem,
    QualityMetricItem,
)
from app.services import quality_case as service

router = APIRouter(dependencies=[Depends(require_expertise_internal_token)])


@router.post("/sync/cases", response_model=QualityCaseResponse)
def sync_case(payload: QualityCaseSyncItem, db: Session = Depends(get_db)):
    return service.sync_case(db, payload.model_dump(exclude_unset=True))


@router.get("/cases", response_model=list[QualityCaseResponse])
def list_cases(
    status: str | None = Query(default=None),
    nomenclature_code: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    return service.list_cases(db, status=status, nomenclature_code=nomenclature_code)


@router.get("/cases/{case_id}", response_model=QualityCaseResponse)
def get_case(case_id: int, db: Session = Depends(get_db)):
    return service.get_case(db, case_id)


@router.get("/cases/{case_id}/history", response_model=list[QualityCaseEventResponse])
def get_history(case_id: int, db: Session = Depends(get_db)):
    return service.get_history(db, case_id)


@router.post("/cases/{case_id}/start-review", response_model=QualityCaseResponse)
def start_review(case_id: int, payload: QualityCaseActionRequest, db: Session = Depends(get_db)):
    return service.start_review(db, case_id=case_id, **payload.model_dump())


@router.post("/cases/{case_id}/decision", response_model=QualityCaseResponse)
def record_decision(
    case_id: int, payload: QualityCaseDecisionRequest, db: Session = Depends(get_db)
):
    return service.record_decision(db, case_id=case_id, **payload.model_dump())


@router.get("/metrics/quality", response_model=list[QualityMetricItem])
def get_quality_metrics(
    date_from: datetime,
    date_to: datetime,
    db: Session = Depends(get_db),
):
    return service.quality_metrics(db, date_from=date_from, date_to=date_to)
