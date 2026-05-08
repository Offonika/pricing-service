from __future__ import annotations

import logging
from datetime import date, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.services.weekly_buyer_digest import (
    WeeklyBuyerDigestService,
    build_weekly_buyer_digest_service,
)

logger = logging.getLogger("app.workers.weekly_buyer_digest")


def _week_bounds(today: date | None = None) -> tuple[date, date]:
    end = today or date.today()
    start = end - timedelta(days=6)
    return start, end


def _get_engine():
    settings = get_settings()
    return create_engine(settings.database_url)


def run_weekly_buyer_digest_job(
    service: WeeklyBuyerDigestService | None = None,
    today: date | None = None,
) -> dict:
    settings = get_settings()
    if not settings.weekly_buyer_digest_enabled:
        logger.info("weekly buyer digest disabled; skipping job")
        return {
            "skipped": True,
            "reason": "feature_disabled",
            "release_count": 0,
            "brand_count": 0,
            "errors": 0,
        }

    week_start, week_end = _week_bounds(today)
    if service:
        return service.generate_weekly_digest(week_start, week_end)

    engine = _get_engine()
    result = {
        "skipped": False,
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        "errors": 0,
    }
    with Session(engine) as session:
        try:
            digest_service = build_weekly_buyer_digest_service(session)
            output = digest_service.generate_weekly_digest(week_start, week_end)
            result.update(output)
        except Exception:
            session.rollback()
            result["errors"] = result.get("errors", 0) + 1
            logger.exception("weekly buyer digest job failed")
    return result
