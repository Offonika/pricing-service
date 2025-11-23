from decimal import Decimal
from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.dependencies import get_db
from app.schemas.reports import PriceChangeItem, SummaryReport
from app.services.reporting import build_price_changes, build_summary

router = APIRouter()


@router.get("/summary", response_model=SummaryReport)
def get_summary(db: Session = Depends(get_db)):
    return build_summary(db)


@router.get("/price-changes", response_model=List[PriceChangeItem])
def get_price_changes(limit: int = 10, db: Session = Depends(get_db)):
    return build_price_changes(db, limit=limit)
