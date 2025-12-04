from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.dependencies import get_db
from app.schemas.market_research import (
    DeviceModelCreate,
    DeviceModelResponse,
    KeywordBulkCreate,
    KeywordResponse,
)
from app.services.market_research.device_models import DeviceModelService
from app.services.market_research.keyword_generation import KeywordGenerationService

router = APIRouter(prefix="/agents", tags=["agents"])


@router.post("/devices/models", response_model=DeviceModelResponse, status_code=status.HTTP_202_ACCEPTED)
def upsert_device_model(payload: DeviceModelCreate, db: Session = Depends(get_db)):
    """
    Приём нормализованных моделей телефонов от агентного контура.
    """
    service = DeviceModelService(db)
    model = service.create_or_update_from_agent(payload)
    db.commit()
    db.refresh(model)
    return model


@router.post("/devices/models/bulk", response_model=List[DeviceModelResponse], status_code=status.HTTP_202_ACCEPTED)
def upsert_device_models_bulk(payload: List[DeviceModelCreate], db: Session = Depends(get_db)):
    """
    Батчевый приём моделей телефонов.
    """
    service = DeviceModelService(db)
    models = [service.create_or_update_from_agent(item) for item in payload]
    db.commit()
    for model in models:
        db.refresh(model)
    return models


@router.post("/keywords/bulk", response_model=List[KeywordResponse], status_code=status.HTTP_202_ACCEPTED)
def bulk_keywords(payload: KeywordBulkCreate, db: Session = Depends(get_db)):
    """
    Приём уже сгенерированных ключевых фраз от агента (если генерация вынесена наружу).
    """
    service = KeywordGenerationService(db)
    keywords = service.bulk_create_from_agent(
        phone_model_id=payload.phone_model_id,
        phrases=payload.phrases,
        language=payload.language,
        category=payload.category,
    )
    db.commit()
    return keywords


@router.post("/keywords/generate", response_model=List[KeywordResponse], status_code=status.HTTP_202_ACCEPTED)
def generate_keywords_for_model(phone_model_id: int, db: Session = Depends(get_db)):
    """
    Генерация ключевых фраз внутри backend по указанной модели телефона.
    """
    device_service = DeviceModelService(db)
    keyword_service = KeywordGenerationService(db)
    device = device_service.get(phone_model_id)
    if not device:
        raise HTTPException(status_code=404, detail="phone model not found")
    keywords = keyword_service.create_keywords_for_device(device)
    db.commit()
    return keywords
