from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.dependencies import get_db
from app.schemas.telegram import TelegramAlert, TelegramItem
from app.services import telegram as tg_service

router = APIRouter()


@router.get("/today", response_model=List[TelegramItem])
def today_summary(
    limit: int = 20,
    brand: list[str] | None = Query(default=None),
    category: list[str] | None = Query(default=None),
    search: str | None = None,
    db: Session = Depends(get_db),
):
    return tg_service.get_today_items(
        db, limit=limit, brands=brand, categories=category, search=search
    )


@router.get("/alerts", response_model=List[TelegramAlert])
def alerts(limit: int = 20, db: Session = Depends(get_db)):
    return tg_service.get_alerts(db, limit=limit)
