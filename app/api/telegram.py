from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.dependencies import get_db
from app.schemas.telegram import TelegramAlert, TelegramItem
from app.services import telegram as tg_service

router = APIRouter()


@router.get("/today", response_model=List[TelegramItem])
def today_summary(
    limit: int = 20,
    brand: Optional[List[str]] = Query(default=None),
    category: Optional[List[str]] = Query(default=None),
    search: Optional[str] = None,
    db: Session = Depends(get_db),
):
    return tg_service.get_today_items(db, limit=limit, brands=brand, categories=category, search=search)


@router.get("/alerts", response_model=List[TelegramAlert])
def alerts(limit: int = 20, db: Session = Depends(get_db)):
    return tg_service.get_alerts(db, limit=limit)
