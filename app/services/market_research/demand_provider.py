from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Keyword, KeywordDemand, PhoneModel


class MarketDemandProvider:
    """
    Поставщик агрегированных метрик спроса для pricing-engine.
    Использует средние показы за заданное окно дней, опционально по региону.
    """

    def __init__(self, db: Session, days_window: int = 30):
        self.db = db
        self.days_window = days_window

    def get_model_demand_score(
        self,
        phone_model_id: Optional[int] = None,
        brand: Optional[str] = None,
        model_name: Optional[str] = None,
        region: Optional[str] = None,
    ) -> Optional[float]:
        query = (
            self.db.query(func.avg(KeywordDemand.impressions))
            .join(Keyword, Keyword.id == KeywordDemand.keyword_id)
            .join(PhoneModel, PhoneModel.id == Keyword.phone_model_id)
            .filter(KeywordDemand.date >= date.today() - timedelta(days=self.days_window))
            .filter(Keyword.is_active.is_(True))
        )
        if region:
            query = query.filter(KeywordDemand.region == region)
        if phone_model_id:
            query = query.filter(PhoneModel.id == phone_model_id)
        elif brand and model_name:
            query = query.filter(PhoneModel.brand == brand, PhoneModel.model_name == model_name)
        else:
            return None
        return query.scalar()
