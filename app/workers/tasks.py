import logging
from typing import Iterable

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Product
from app.services.pricing import (
    calculate_recommendation,
    get_or_create_strategy_version,
    record_recommendation,
)

logger = logging.getLogger("app.workers.pricing")


def get_engine():
    settings = get_settings()
    return create_engine(settings.database_url)


def recalculate_all_prices(product_skus: Iterable[str] | None = None) -> dict:
    """
    Простая заглушка фона: перебирает продукты и считает рекомендации.
    В реальном Celery таске engine/session будут создаваться на воркере.
    """
    engine = get_engine()
    results = {"processed": 0, "errors": 0}
    with Session(engine) as session:
        strategy_version = get_or_create_strategy_version(session)
        session.commit()
        query = session.query(Product)
        if product_skus:
            query = query.filter(Product.sku.in_(product_skus))
        for product in query.all():
            try:
                rec = calculate_recommendation(product, competitor_min_price=None)
                record_recommendation(
                    session,
                    product,
                    rec,
                    strategy_version=strategy_version,
                    competitor_min_price=None,
                )
                logger.info(
                    "price recalculated",
                    extra={"sku": product.sku, "recommended": str(rec.recommended_price)},
                )
                results["processed"] += 1
            except Exception:
                logger.exception("failed to recalc price", extra={"sku": product.sku})
                session.rollback()
                results["errors"] += 1
            else:
                session.commit()
    return results
