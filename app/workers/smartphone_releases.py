import json
import logging
from typing import Optional

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.services.smartphone_releases import SmartphoneReleaseService, build_release_service
from app.models import SmartphoneRelease
from sqlalchemy import desc
from app.services.market_research import DeviceModelService, KeywordGenerationService
from app.schemas.market_research import DeviceModelCreate

logger = logging.getLogger("app.workers.smartphone_releases")


def get_engine():
    settings = get_settings()
    return create_engine(settings.database_url)


def run_smartphone_release_job(service: Optional[SmartphoneReleaseService] = None) -> dict:
    """
    Обновляет таблицу smartphone_releases:
    - проверяет фича-флаг SMARTPHONE_RELEASES_ENABLED,
    - вытаскивает новости, нормализует LLM и делает upsert в БД.
    """
    settings = get_settings()
    if not settings.smartphone_releases_enabled:
        logger.info("smartphone releases feature disabled; skipping job")
        return {
            "skipped": True,
            "fetched": 0,
            "processed": 0,
            "created": 0,
            "updated": 0,
            "errors": 0,
            "highlights": [],
        }

    if service:
        result = service.ingest_latest()
        result["skipped"] = False
        result.setdefault("highlights", [])
        return result

    engine = get_engine()
    result = {
        "skipped": False,
        "fetched": 0,
        "processed": 0,
        "created": 0,
        "updated": 0,
        "errors": 0,
        "highlights": [],
    }
    with Session(engine) as session:
        try:
            release_service = build_release_service(session)
            result.update(release_service.ingest_latest())
            sync_stats = sync_releases_to_phone_models(session)
            result.update({f"sync_{k}": v for k, v in sync_stats.items()})
        except Exception:
            session.rollback()
            result["errors"] += 1
            logger.exception("smartphone release job failed")
    return result


def sync_releases_to_phone_models(session: Session, limit: int = 50) -> dict:
    """
    Приводит свежие smartphone_releases (announced/released) к PhoneModel и генерирует ключи.
    """
    releases = (
        session.query(SmartphoneRelease)
        .filter(
            SmartphoneRelease.is_active.is_(True),
            SmartphoneRelease.brand.isnot(None),
            SmartphoneRelease.model.isnot(None),
            SmartphoneRelease.release_status.in_(["announced", "released", None]),
        )
        .order_by(desc(SmartphoneRelease.created_at))
        .limit(limit)
        .all()
    )
    device_service = DeviceModelService(session)
    keyword_service = KeywordGenerationService(session)
    stats = {"synced_models": 0, "updated_models": 0, "keywords_created": 0}

    for rel in releases:
        existing = device_service.repo.get_by_brand_model_variant(rel.brand, rel.model, None)
        payload = DeviceModelCreate(
            brand=rel.brand,
            model_name=rel.model,
            variant=None,
            announce_date=rel.announcement_date,
            release_date=rel.market_release_date,
        )
        device = device_service.create_or_update_from_agent(payload)
        if existing:
            stats["updated_models"] += 1
        else:
            stats["synced_models"] += 1
        if not device.keywords:
            created_keywords = keyword_service.create_keywords_for_device(device)
            stats["keywords_created"] += len(created_keywords)
    session.commit()
    if any(stats.values()):
        logger.info("synced releases to phone models", extra=stats)
    return stats


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    output = run_smartphone_release_job()
    print(json.dumps(output, ensure_ascii=False))
