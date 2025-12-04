from __future__ import annotations

from datetime import date, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.dependencies import get_db
from app.schemas.analytics import ModelDemandItem, ModelDemandTimeseriesItem
from app.services.analytics import ModelDemandService

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/model-demand/top", response_model=List[ModelDemandItem])
def get_top_model_demand(
    days: int = Query(default=30, ge=1, le=180),
    limit: int = Query(default=50, ge=1, le=500),
    brand: Optional[str] = None,
    region: Optional[str] = None,
    db: Session = Depends(get_db),
):
    service = ModelDemandService(db)
    return service.get_top_models(days=days, limit=limit, brand=brand, region=region)


@router.get("/model-demand/{device_model_id}/timeseries", response_model=List[ModelDemandTimeseriesItem])
def get_model_demand_timeseries(
    device_model_id: int,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    region: Optional[str] = None,
    db: Session = Depends(get_db),
):
    if date_to is None:
        date_to = date.today()
    if date_from is None:
        date_from = date_to - timedelta(days=30)
    if date_from > date_to:
        raise HTTPException(status_code=400, detail="date_from must be before date_to")
    service = ModelDemandService(db)
    return service.get_timeseries(
        device_model_id=device_model_id,
        date_from=date_from,
        date_to=date_to,
        region=region,
    )
