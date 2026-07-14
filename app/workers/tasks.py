from __future__ import annotations

import logging
from collections.abc import Iterable

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.infrastructure.db import get_application_engine
from app.models import Product
from app.services.market_research import MarketDemandProvider
from app.services.pricing import (
    calculate_recommendation,
    get_or_create_strategy_version,
    record_recommendation,
)

logger = logging.getLogger("app.workers.pricing")


def get_engine():
    return get_application_engine()


def recalculate_all_prices(product_articles: Iterable[str] | None = None) -> dict:
    """
    Простая заглушка фона: перебирает продукты и считает рекомендации.
    В реальном Celery таске engine/session будут создаваться на воркере.
    """
    engine = get_engine()
    settings = get_settings()
    results = {"processed": 0, "errors": 0}
    with Session(engine) as session:
        strategy_version = get_or_create_strategy_version(session)
        session.commit()
        demand_provider: MarketDemandProvider | None = None
        if settings.feature_yandex_demand_enabled:
            demand_provider = MarketDemandProvider(
                session, days_window=settings.yandex_demand_days_window
            )
        query = session.query(Product)
        if product_articles:
            query = query.filter(Product.article.in_(product_articles))
        for product in query.all():
            try:
                demand_score = None
                if demand_provider and product.brand and product.name:
                    demand_score = demand_provider.get_model_demand_score(
                        brand=product.brand, model_name=product.name
                    )
                    if demand_score is not None:
                        logger.info(
                            "demand signal fetched",
                            extra={
                                "article": product.article,
                                "brand": product.brand,
                                "model_name": product.name,
                                "avg_impressions": demand_score,
                            },
                        )
                rec = calculate_recommendation(
                    product, competitor_min_price=None, demand_score=demand_score
                )
                record_recommendation(
                    session,
                    product,
                    rec,
                    strategy_version=strategy_version,
                    competitor_min_price=None,
                )
                logger.info(
                    "price recalculated",
                    extra={"article": product.article, "recommended": str(rec.recommended_price)},
                )
                results["processed"] += 1
            except Exception:
                logger.exception("failed to recalc price", extra={"article": product.article})
                session.rollback()
                results["errors"] += 1
            else:
                session.commit()
    return results
