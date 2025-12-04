from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Product
from app.services.market_research import ProductModelMatcher

logger = logging.getLogger("app.workers.matching")


def get_engine():
    settings = get_settings()
    return create_engine(settings.database_url)


def match_products_to_phone_models(limit: Optional[int] = None):
    """
    Простая пакетная сверка Product ↔ PhoneModel на основе бренда/подстроки модели.
    Результаты пока логируются; сохранение в таблицу сопоставлений при необходимости можно добавить отдельно.
    """
    engine = get_engine()
    results = {"processed": 0, "matched": 0}
    with Session(engine) as session:
        matcher = ProductModelMatcher(session)
        query = session.query(Product)
        if limit:
            query = query.limit(limit)
        for product in query.all():
            match = matcher.match_product(product)
            results["processed"] += 1
            if match:
                results["matched"] += 1
                logger.info(
                    "product matched to phone model",
                    extra={
                        "product_id": match.product_id,
                        "phone_model_id": match.phone_model_id,
                        "confidence": match.confidence,
                        "reason": match.reason,
                    },
                )
    return results
