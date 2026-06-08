from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.services.receivables import (
    OneCReceivableLedgerExtractor,
    fetch_employee_counterparty_refs_from_onec,
    sync_receivable_ledger,
)


def _get_app_engine():
    return create_engine(get_settings().database_url)


def _get_onec_engine():
    settings = get_settings()
    if not settings.onec_database_url:
        raise RuntimeError("ONEC_DATABASE_URL is not configured")
    return create_engine(settings.onec_database_url)


def run_receivable_ledger_sync(
    *,
    operations_sql: str,
    snapshot_date: date | None = None,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    employee_counterparty_refs: tuple[str, ...] = (),
    fired_manager_refs: tuple[str, ...] = (),
) -> dict:
    onec_engine = _get_onec_engine()
    resolved_employee_refs = (
        employee_counterparty_refs or fetch_employee_counterparty_refs_from_onec(onec_engine)
    )
    extractor = OneCReceivableLedgerExtractor(onec_engine, operations_sql=operations_sql)
    events = extractor.fetch_receivable_events(window_start=window_start, window_end=window_end)

    with Session(_get_app_engine()) as session:
        result = sync_receivable_ledger(
            session,
            events,
            snapshot_date=snapshot_date,
            employee_counterparty_refs=resolved_employee_refs,
            fired_manager_refs=fired_manager_refs,
        )
        session.commit()
    result["fetched_events"] = len(events)
    result["employee_counterparty_ref_count"] = len(resolved_employee_refs)
    return result
