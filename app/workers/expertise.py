from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.infrastructure.db import build_onec_engine_from_settings, get_application_engine
from app.models import ExpertiseCase
from app.services import expertise as expertise_service
from app.services import expertise_bitrix
from app.services.expertise_onec import OneCExpertiseExtractor, load_expertise_onec_sql


def _get_app_engine():
    return get_application_engine()


def _get_onec_engine():
    settings = get_settings()
    if not settings.onec_database_url:
        raise RuntimeError("ONEC_DATABASE_URL is not configured")
    return build_onec_engine_from_settings(poolclass=NullPool)


def run_expertise_onec_sync(
    extractor: OneCExpertiseExtractor | None = None,
) -> dict[str, int | str]:
    settings = get_settings()
    if extractor is None:
        extractor = OneCExpertiseExtractor(
            _get_onec_engine(),
            sql=load_expertise_onec_sql(settings),
        )

    payloads = extractor.fetch_case_payloads()
    with Session(_get_app_engine()) as session:
        result = expertise_service.sync_cases(session, payloads)
    result["fetched"] = len(payloads)
    return result


def run_expertise_bitrix_sync(*, only_failed: bool = False) -> dict[str, int]:
    with Session(_get_app_engine()) as session:
        return expertise_bitrix.sync_pending_cases(session, only_failed=only_failed)


def run_expertise_alarm_scan() -> dict[str, int]:
    with Session(_get_app_engine()) as session:
        return expertise_bitrix.scan_alarm_cases(session)


def run_expertise_completion_outcome_backfill() -> dict[str, int]:
    with Session(_get_app_engine()) as session:
        case_ids = session.scalars(
            select(ExpertiseCase.id).where(
                ExpertiseCase.current_status == expertise_service.STATUS_RETURNED_TO_STORE,
                ExpertiseCase.decision_code == expertise_service.DECISION_APPROVED,
            )
        ).all()
        result = expertise_service.backfill_completion_outcomes(session)
        synced = 0
        errors = 0
        for case_id in case_ids:
            try:
                expertise_bitrix.sync_case_to_bitrix(session, case_id=case_id)
                session.commit()
                synced += 1
            except Exception:
                session.rollback()
                errors += 1
        return {
            "updated": result["updated"],
            "synced": synced,
            "errors": errors,
        }
