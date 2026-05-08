from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.dependencies import get_db, require_management_internal_token
from app.schemas.management import (
    ReceivableCaseItem,
    ReceivablesCaseListResponse,
    ReceivablesManagerSummaryItem,
    ReceivablesManagerSummaryResponse,
)
from app.services.receivables import (
    CASE_EMPLOYEE,
    CASE_NEW_DAILY,
    list_receivable_cases,
    summarize_receivables_by_manager,
)

router = APIRouter()


def _build_case_response(
    snapshot_date: date, items: list[ReceivableCaseItem]
) -> ReceivablesCaseListResponse:
    return ReceivablesCaseListResponse(
        as_of=snapshot_date,
        freshness_status="fresh" if items else "missing",
        source_status="ready" if items else "empty",
        payload=items,
    )


@router.get("/new-daily", response_model=ReceivablesCaseListResponse)
def list_new_daily_receivables(
    date_value: date = Query(alias="date"),
    db: Session = Depends(get_db),
    _: str = Depends(require_management_internal_token),
):
    items = [
        ReceivableCaseItem.model_validate(item, from_attributes=True)
        for item in list_receivable_cases(db, snapshot_date=date_value, segment=CASE_NEW_DAILY)
    ]
    return _build_case_response(date_value, items)


@router.get("/cases", response_model=ReceivablesCaseListResponse)
def list_receivables_cases(
    date_value: date = Query(alias="date"),
    segment: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _: str = Depends(require_management_internal_token),
):
    items = [
        ReceivableCaseItem.model_validate(item, from_attributes=True)
        for item in list_receivable_cases(db, snapshot_date=date_value, segment=segment)
    ]
    return _build_case_response(date_value, items)


@router.get("/employee-cases", response_model=ReceivablesCaseListResponse)
def list_employee_receivable_cases(
    date_value: date = Query(alias="date"),
    db: Session = Depends(get_db),
    _: str = Depends(require_management_internal_token),
):
    items = [
        ReceivableCaseItem.model_validate(item, from_attributes=True)
        for item in list_receivable_cases(db, snapshot_date=date_value, segment=CASE_EMPLOYEE)
    ]
    return _build_case_response(date_value, items)


@router.get("/manager-summary", response_model=ReceivablesManagerSummaryResponse)
def get_receivables_manager_summary(
    date_value: date = Query(alias="date"),
    db: Session = Depends(get_db),
    _: str = Depends(require_management_internal_token),
):
    payload = [
        ReceivablesManagerSummaryItem.model_validate(item)
        for item in summarize_receivables_by_manager(db, snapshot_date=date_value)
    ]
    return ReceivablesManagerSummaryResponse(
        as_of=date_value,
        freshness_status="fresh" if payload else "missing",
        source_status="ready" if payload else "empty",
        payload=payload,
    )
