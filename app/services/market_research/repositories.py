from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable, List, Optional, Sequence

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.models import Keyword, KeywordDemand, PhoneModel


class DeviceModelRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_by_brand_model_variant(
        self, brand: str, model_name: str, variant: Optional[str]
    ) -> Optional[PhoneModel]:
        return (
            self.db.query(PhoneModel)
            .filter(
                PhoneModel.brand == brand,
                PhoneModel.model_name == model_name,
                PhoneModel.variant.is_(variant) if variant is None else PhoneModel.variant == variant,
            )
            .first()
        )

    def create(self, payload: dict) -> PhoneModel:
        model = PhoneModel(**payload)
        self.db.add(model)
        self.db.flush()
        return model

    def update(self, model: PhoneModel, payload: dict) -> PhoneModel:
        for key, value in payload.items():
            setattr(model, key, value)
        self.db.add(model)
        self.db.flush()
        return model

    def list_without_keywords(self, limit: int = 100) -> List[PhoneModel]:
        subq = self.db.query(Keyword.phone_model_id).distinct()
        return (
            self.db.query(PhoneModel)
            .filter(~PhoneModel.id.in_(subq))
            .order_by(PhoneModel.created_at.desc())
            .limit(limit)
            .all()
        )


class KeywordRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_by_phrase_and_model(self, phrase: str, phone_model_id: int) -> Optional[Keyword]:
        return (
            self.db.query(Keyword)
            .filter(Keyword.phrase == phrase, Keyword.phone_model_id == phone_model_id)
            .first()
        )

    def create_many(
        self,
        phone_model_id: int,
        phrases: Sequence[str],
        language: Optional[str],
        category: Optional[str],
        source: Optional[str],
    ) -> List[Keyword]:
        items: List[Keyword] = []
        for phrase in phrases:
            existing = self.get_by_phrase_and_model(phrase, phone_model_id)
            if existing:
                items.append(existing)
                continue
            keyword = Keyword(
                phrase=phrase,
                phone_model_id=phone_model_id,
                language=language or "ru",
                category=category or "display",
                source=source,
            )
            self.db.add(keyword)
            items.append(keyword)
        self.db.flush()
        return items

    def list_for_demand_update(self, limit: int = 100) -> List[Keyword]:
        return (
            self.db.query(Keyword)
            .filter(Keyword.is_active.is_(True))
            .order_by(Keyword.updated_at.desc())
            .limit(limit)
            .all()
        )

    def list_stale_for_demand(self, staleness_days: int, limit: int) -> List[Keyword]:
        """
        Возвращает активные ключи, у которых нет данных спроса или данные старше staleness_days.
        """
        subq = (
            self.db.query(KeywordDemand.keyword_id, func.max(KeywordDemand.date).label("last_date"))
            .group_by(KeywordDemand.keyword_id)
            .subquery()
        )
        threshold = date.today() - timedelta(days=staleness_days)
        query = (
            self.db.query(Keyword)
            .outerjoin(subq, Keyword.id == subq.c.keyword_id)
            .filter(Keyword.is_active.is_(True))
            .filter(or_(subq.c.last_date.is_(None), subq.c.last_date < threshold))
            .order_by(Keyword.created_at.desc())
            .limit(limit)
        )
        return query.all()


class KeywordDemandRepository:
    def __init__(self, db: Session):
        self.db = db

    def create_many(self, stats: Iterable[KeywordDemand]) -> List[KeywordDemand]:
        saved: List[KeywordDemand] = []
        for item in stats:
            self.db.add(item)
            saved.append(item)
        self.db.flush()
        return saved

    def last_for_keyword(self, keyword_id: int) -> Optional[KeywordDemand]:
        return (
            self.db.query(KeywordDemand)
            .filter(KeywordDemand.keyword_id == keyword_id)
            .order_by(KeywordDemand.date.desc())
            .first()
        )

    def aggregate_impressions(
        self,
        phone_model_id: Optional[int],
        brand: Optional[str],
        model_name: Optional[str],
        days: int,
        region: Optional[str] = None,
    ) -> Optional[float]:
        query = (
            self.db.query(func.avg(KeywordDemand.impressions))
            .join(Keyword, Keyword.id == KeywordDemand.keyword_id)
            .join(PhoneModel, PhoneModel.id == Keyword.phone_model_id)
            .filter(KeywordDemand.date >= date.today() - timedelta(days=days))
            .filter(Keyword.is_active.is_(True))
        )
        if region:
            query = query.filter(or_(KeywordDemand.region == region, KeywordDemand.region.is_(None)))
        if phone_model_id:
            query = query.filter(PhoneModel.id == phone_model_id)
        elif brand and model_name:
            query = query.filter(PhoneModel.brand == brand, PhoneModel.model_name == model_name)
        else:
            return None
        return query.scalar()
