from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.infrastructure.db import build_onec_engine_from_settings, get_application_engine
from app.services.onec_sales_kpi import fetch_onec_daily_sales_kpi, sync_onec_daily_sales_kpi


def _get_app_engine():
    return get_application_engine()


def _get_onec_engine():
    settings = get_settings()
    if not settings.onec_database_url:
        raise RuntimeError("ONEC_DATABASE_URL is not configured")
    return build_onec_engine_from_settings()


def run_onec_sales_kpi_sync(*, date_from: date, date_to: date) -> dict[str, int | str]:
    rows = fetch_onec_daily_sales_kpi(_get_onec_engine(), date_from=date_from, date_to=date_to)
    with Session(_get_app_engine()) as session:
        result = sync_onec_daily_sales_kpi(
            session,
            rows=rows,
            date_from=date_from,
            date_to=date_to,
        )
        session.commit()
    result["date_from"] = date_from.isoformat()
    result["date_to"] = date_to.isoformat()
    result["fetched"] = len(rows)
    return result
