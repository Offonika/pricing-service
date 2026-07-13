from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.infrastructure.db import build_onec_engine_from_settings, get_application_engine
from app.services.counterparty_duplicates import run_counterparty_duplicate_detection


def run_counterparty_duplicate_job(
    *,
    run_at: datetime | None = None,
    sql_text: str | None = None,
) -> dict[str, object]:
    settings = get_settings()
    if not settings.counterparty_duplicate_enabled and sql_text is None:
        return {"enabled": False}

    engine = get_application_engine()
    onec_engine = build_onec_engine_from_settings() if settings.onec_database_url else None
    try:
        with Session(engine) as session:
            result = run_counterparty_duplicate_detection(
                session,
                run_at=run_at,
                onec_engine=onec_engine,
                sql_text=sql_text,
            )
            session.commit()
            return result
    finally:
        engine.dispose()
        if onec_engine is not None:
            onec_engine.dispose()
