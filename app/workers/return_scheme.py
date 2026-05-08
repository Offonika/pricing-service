from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.services.return_scheme import (
    OneCReturnSchemeExtractor,
    build_return_scheme_output_path,
    create_return_scheme_alert_batch,
    detect_return_scheme_incidents,
    export_return_scheme_report_xlsx,
    parse_retail_price_types,
    upsert_return_scheme_incidents,
)

logger = logging.getLogger("app.workers.return_scheme")


def _get_app_engine():
    settings = get_settings()
    return create_engine(settings.database_url)


def _get_onec_engine():
    settings = get_settings()
    if not settings.onec_database_url:
        raise RuntimeError("ONEC_DATABASE_URL is not configured")
    return create_engine(settings.onec_database_url)


def run_return_scheme_job(
    extractor: OneCReturnSchemeExtractor | None = None,
    *,
    now: datetime | None = None,
) -> dict:
    settings = get_settings()
    if not settings.return_scheme_enabled:
        logger.info("return scheme monitoring disabled; skipping job")
        return {
            "skipped": True,
            "reason": "feature_disabled",
            "fetched_events": 0,
            "detected_incidents": 0,
            "new_incidents": 0,
            "notification_incidents": 0,
            "errors": 0,
        }

    run_at = now or datetime.now()
    window_end = run_at
    window_start = run_at - timedelta(days=settings.return_scheme_window_days)
    retail_price_types = parse_retail_price_types(settings.return_scheme_retail_price_types)

    result = {
        "skipped": False,
        "generated_at": run_at.isoformat(),
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "fetched_events": 0,
        "detected_incidents": 0,
        "new_incidents": 0,
        "notification_incidents": 0,
        "notification_incident_ids": [],
        "batch_id": None,
        "report_path": None,
        "errors": 0,
    }

    try:
        if extractor is None:
            extractor = OneCReturnSchemeExtractor(_get_onec_engine())
        events = extractor.fetch_operation_events(window_start=window_start, window_end=window_end)
        result["fetched_events"] = len(events)
        incidents = detect_return_scheme_incidents(
            events,
            retail_price_types=retail_price_types,
            window_days=settings.return_scheme_window_days,
        )
        result["detected_incidents"] = len(incidents)

        app_engine = _get_app_engine()
        with Session(app_engine) as session:
            persisted = upsert_return_scheme_incidents(session, incidents, detected_at=run_at)
            result["new_incidents"] = len(persisted["new"])
            pending_notification = [
                incident
                for incident in persisted["pending_notification"]
                if incident.alert_batch_id is None and incident.notified_at is None
            ]
            result["notification_incidents"] = len(pending_notification)
            result["notification_incident_ids"] = [incident.id for incident in pending_notification]

            if pending_notification:
                output_path = build_return_scheme_output_path(
                    output_dir=settings.return_scheme_output_dir,
                    generated_at=run_at,
                )
                export_return_scheme_report_xlsx(pending_notification, output_path)
                result["report_path"] = str(Path(output_path).resolve())
                batch = create_return_scheme_alert_batch(
                    session,
                    incidents=pending_notification,
                    generated_at=run_at,
                    window_start=window_start,
                    window_end=window_end,
                    report_path=output_path,
                    new_incidents_count=result["new_incidents"],
                )
                if batch is not None:
                    result["batch_id"] = batch.id

            session.commit()
    except Exception:
        logger.exception("return scheme monitoring job failed")
        result["errors"] += 1

    return result
