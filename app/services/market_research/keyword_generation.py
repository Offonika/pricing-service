from typing import List, Optional

from sqlalchemy.orm import Session

from app.models import Keyword, PhoneModel
from app.services.market_research.repositories import KeywordRepository


class KeywordGenerationService:
    """Генерация и сохранение поисковых фраз под запчасти для моделей телефонов."""

    def __init__(self, db: Session):
        self.db = db
        self.repo = KeywordRepository(db)

    def generate_for_device(self, device: PhoneModel) -> List[str]:
        """
        Формирует список текстовых фраз по шаблонам:
        - дисплей/экран/тач/стекло <brand> <model> (+варианты купить/оригинал/замена и т.п.).
        """
        full_name_parts = [device.brand or "", device.model_name or "", device.variant or ""]
        full_name = " ".join(part for part in full_name_parts if part).strip()
        normalized = full_name.lower()
        templates = [
            "дисплей {name}",
            "экран {name}",
            "экран {name} купить",
            "замена дисплея {name}",
            "замена экрана {name}",
            "модуль экрана {name} оригинал",
            "дисплей {name} оригинал",
            "тачскрин {name}",
        ]
        return list({tpl.format(name=normalized) for tpl in templates})

    def create_keywords_for_device(self, device: PhoneModel) -> List[Keyword]:
        """Генерирует и сохраняет фразы в БД, избегая дублей."""
        phrases = self.generate_for_device(device)
        return self.repo.create_many(
            phone_model_id=device.id,
            phrases=phrases,
            language="ru",
            category="display",
            source="backend",
        )

    def bulk_create_from_agent(
        self, phone_model_id: int, phrases: List[str], language: Optional[str], category: Optional[str]
    ) -> List[Keyword]:
        """Сохраняет фразы, переданные агентом напрямую."""
        cleaned = [p.strip() for p in phrases if p and p.strip()]
        return self.repo.create_many(
            phone_model_id=phone_model_id,
            phrases=cleaned,
            language=language or "ru",
            category=category or "display",
            source="agent",
        )
