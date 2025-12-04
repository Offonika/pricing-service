from typing import List, Optional

from sqlalchemy.orm import Session

from app.models import PhoneModel
from app.schemas.market_research import DeviceModelCreate
from app.services.market_research.repositories import DeviceModelRepository


class DeviceModelService:
    """Сервис управления моделями телефонов, наполнение из агентного контура."""

    def __init__(self, db: Session):
        self.db = db
        self.repo = DeviceModelRepository(db)

    def create_or_update_from_agent(self, payload: DeviceModelCreate) -> PhoneModel:
        """
        Принимает нормализованные данные от агента и:
        - создаёт новую модель телефона;
        - либо обновляет существующую по brand + model_name + variant.
        """
        existing = self.repo.get_by_brand_model_variant(payload.brand, payload.model_name, payload.variant)
        screen_payload = {}
        if payload.screen:
            screen_payload = {
                "screen_size_inch": payload.screen.size_inch,
                "screen_technology": payload.screen.technology,
                "screen_refresh_rate_hz": payload.screen.refresh_rate_hz,
            }
        data = {
            "brand": payload.brand,
            "model_name": payload.model_name,
            "variant": payload.variant,
            "announce_date": payload.announce_date,
            "release_date": payload.release_date,
            **screen_payload,
        }
        if existing:
            # Не затираем заполненные поля None-значениями, если агент их не прислал.
            cleaned = {}
            for key, value in data.items():
                if value is None and getattr(existing, key) is not None:
                    continue
                cleaned[key] = value
            return self.repo.update(existing, cleaned)
        return self.repo.create(data)

    def list_new_models_for_keywords(self, limit: int = 100) -> List[PhoneModel]:
        """Возвращает модели, по которым ещё не сгенерированы ключевые фразы."""
        return self.repo.list_without_keywords(limit=limit)

    def get(self, phone_model_id: int) -> Optional[PhoneModel]:
        """Возвращает модель телефона по id или None."""
        return self.db.get(PhoneModel, phone_model_id)
