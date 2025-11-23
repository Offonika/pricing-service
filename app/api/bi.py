from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.dependencies import get_db
from app.schemas.bi import BICompetitorPrice, BIProduct, BIRecommendation
from app.services import bi as bi_service

router = APIRouter()


@router.get("/products", response_model=List[BIProduct])
def bi_products(limit: int = 100, db: Session = Depends(get_db)):
    return bi_service.get_products_dataset(db, limit=limit)


@router.get("/recommendations", response_model=List[BIRecommendation])
def bi_recommendations(limit: int = 100, db: Session = Depends(get_db)):
    return bi_service.get_latest_recommendations(db, limit=limit)


@router.get("/competitor-prices", response_model=List[BICompetitorPrice])
def bi_competitor_prices(limit: int = 100, db: Session = Depends(get_db)):
    return bi_service.get_competitor_prices(db, limit=limit)
