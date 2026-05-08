from __future__ import annotations

from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.services.counterparty_duplicates import run_counterparty_duplicate_detection


def run_counterparty_duplicate_job(
    *,
    run_at: datetime | None = None,
    sql_text: str | None = None,
) -> dict[str, object]:
    settings = get_settings()
    if not settings.counterparty_duplicate_enabled and sql_text is None:
        return {"enabled": False}

    engine = create_engine(settings.database_url)
    onec_engine = create_engine(settings.onec_database_url) if settings.onec_database_url else None
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
