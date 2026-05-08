from __future__ import annotations

from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.services.telephony import (
    attach_bitrix_metadata,
    attach_staffing_metadata,
    fetch_onec_telephony_user_line_rows,
    sync_telephony_user_line_snapshot,
)


def _get_app_engine():
    return create_engine(get_settings().database_url)


def _get_onec_engine():
    settings = get_settings()
    if not settings.onec_database_url:
        raise RuntimeError("ONEC_DATABASE_URL is not configured")
    return create_engine(
        settings.onec_database_url,
        connect_args={
            "timeout": float(settings.onec_query_timeout_seconds),
            "login_timeout": float(settings.onec_login_timeout_seconds),
        },
    )


def _get_mdm_engine():
    settings = get_settings()
    if not settings.telephony_mdm_database_url:
        return None
    return create_engine(settings.telephony_mdm_database_url)


def run_telephony_user_line_sync(*, snapshot_date: date | None = None) -> dict[str, int | str]:
    effective_snapshot_date = snapshot_date or date.today()
    onec_engine = _get_onec_engine()
    mdm_engine = _get_mdm_engine()
    app_engine = _get_app_engine()

    try:
        rows = fetch_onec_telephony_user_line_rows(
            onec_engine,
            snapshot_date=effective_snapshot_date,
        )
        with Session(app_engine) as session:
            staffing_matches = attach_staffing_metadata(session, rows)
            bitrix_matches = attach_bitrix_metadata(rows, mdm_engine=mdm_engine)
            result = sync_telephony_user_line_snapshot(
                session,
                rows=rows,
                snapshot_date=effective_snapshot_date,
            )
            session.commit()
    finally:
        app_engine.dispose()
        onec_engine.dispose()
        if mdm_engine is not None:
            mdm_engine.dispose()

    return {
        **result,
        "staffing_matches": staffing_matches,
        "bitrix_matches": bitrix_matches,
        "mdm_lookup_enabled": "true" if mdm_engine is not None else "false",
    }
