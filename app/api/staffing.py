from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.dependencies import get_db, require_management_internal_token
from app.schemas.management import (
    StaffingDailyItem,
    StaffingDailyResponse,
    StaffingPeriodSummaryItem,
    StaffingPeriodSummaryResponse,
)
from app.services.staffing import build_staffing_period_summary, list_staffing_snapshots

router = APIRouter()


@router.get("/daily", response_model=StaffingDailyResponse)
def get_staffing_daily(
    date_value: date = Query(alias="date"),
    db: Session = Depends(get_db),
    _: str = Depends(require_management_internal_token),
):
    payload = [
        StaffingDailyItem.model_validate(item, from_attributes=True)
        for item in list_staffing_snapshots(db, snapshot_date=date_value)
    ]
    return StaffingDailyResponse(
        as_of=date_value,
        freshness_status="fresh" if payload else "missing",
        source_status="ready" if payload else "empty",
        payload=payload,
    )


@router.get("/period-summary", response_model=StaffingPeriodSummaryResponse)
def get_staffing_period_summary(
    date_from: date,
    date_to: date,
    db: Session = Depends(get_db),
    _: str = Depends(require_management_internal_token),
):
    payload = [
        StaffingPeriodSummaryItem.model_validate(item)
        for item in build_staffing_period_summary(
            db,
            date_from=date_from,
            date_to=date_to,
            forecast_anchor_date=date_to,
        )
    ]
    return StaffingPeriodSummaryResponse(
        as_of=date_to,
        freshness_status="fresh" if payload else "missing",
        source_status="ready" if payload else "empty",
        payload=payload,
    )
