import logging

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.services.market_research import KeywordRepository, build_demand_service

logger = logging.getLogger("app.workers.demand")


def get_engine():
    settings = get_settings()
    return create_engine(settings.database_url)


def update_stale_keyword_demand():
    """
    Обновляет спрос по ключам, у которых нет данных или данные старше заданного порога.
    Учитывает фича-флаг FEATURE_YANDEX_DEMAND_ENABLED.
    """
    settings = get_settings()
    if not settings.feature_yandex_demand_enabled:
        logger.info("yandex demand feature disabled; skipping demand refresh")
        return {"processed": 0, "saved": 0, "skipped": True}

    engine = get_engine()
    result = {"processed": 0, "saved": 0, "skipped": False, "errors": 0}
    with Session(engine) as session:
        repo = KeywordRepository(session)
        keywords = repo.list_stale_for_demand(
            staleness_days=settings.yandex_demand_staleness_days,
            limit=settings.yandex_demand_update_limit,
        )
        if not keywords:
            logger.info("no stale keywords found for demand refresh")
            return result

        demand_service = build_demand_service(session, batch_size=settings.yandex_direct_batch_size)
        try:
            saved = demand_service.update_demand_for_keywords(
                keywords=keywords, region=settings.yandex_default_region
            )
            session.commit()
            result["processed"] = len(keywords)
            result["saved"] = len(saved)
            logger.info(
                "demand refresh completed",
                extra={
                    "keywords": len(keywords),
                    "saved": len(saved),
                    "region": settings.yandex_default_region,
                },
            )
        except Exception:
            session.rollback()
            result["errors"] += 1
            logger.exception("failed to refresh demand for keywords")
    return result
