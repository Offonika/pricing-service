from __future__ import annotations

from datetime import date, datetime, time, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.services.receivables import (
    OneCReceivableLedgerExtractor,
    fetch_employee_counterparty_refs_from_onec,
    fetch_staff_members_from_onec,
    sync_receivable_ledger,
)
from app.services.staffing import StaffMemberRow, upsert_staff_members

DEFAULT_RECEIVABLE_WINDOW_CHUNK_DAYS = 1


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


def _build_receivable_sync_windows(
    *,
    window_start: datetime | None,
    window_end: datetime | None,
    snapshot_date: date | None,
) -> list[tuple[datetime | None, datetime | None]]:
    settings = get_settings()
    effective_window_end = window_end
    if effective_window_end is None and snapshot_date is not None:
        effective_window_end = datetime.combine(snapshot_date + timedelta(days=1), time.min)

    if window_start is None or effective_window_end is None:
        return [(window_start, effective_window_end)]

    chunk_days = max(
        int(getattr(settings, "receivable_ledger_window_chunk_days", 0) or 0),
        DEFAULT_RECEIVABLE_WINDOW_CHUNK_DAYS,
    )
    chunk_size = timedelta(days=chunk_days)
    if effective_window_end - window_start <= chunk_size:
        return [(window_start, effective_window_end)]

    windows: list[tuple[datetime, datetime]] = []
    current_start = window_start
    while current_start < effective_window_end:
        current_end = min(current_start + chunk_size, effective_window_end)
        windows.append((current_start, current_end))
        current_start = current_end
    return windows


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
    app_engine = _get_app_engine()
    resolved_employee_refs = (
        employee_counterparty_refs or fetch_employee_counterparty_refs_from_onec(onec_engine)
    )
    resolved_staff_rows = [
        StaffMemberRow.from_mapping(item, default_source="onec_physical_person")
        for item in fetch_staff_members_from_onec(onec_engine)
    ]
    extractor = OneCReceivableLedgerExtractor(onec_engine, operations_sql=operations_sql)
    sync_windows = _build_receivable_sync_windows(
        window_start=window_start,
        window_end=window_end,
        snapshot_date=snapshot_date,
    )

    aggregate_result = {
        "processed": 0,
        "inserted": 0,
        "updated": 0,
        "existing": 0,
        "assignments": 0,
        "snapshots": 0,
        "cases": 0,
        "case_segments": {},
        "reset": {
            "ledger_events_deleted": 0,
            "manager_assignments_deleted": 0,
            "snapshots_deleted": 0,
            "cases_deleted": 0,
        },
    }
    staff_result = {"processed": 0, "inserted": 0, "updated": 0}
    with Session(app_engine) as staff_session:
        staff_result = upsert_staff_members(staff_session, resolved_staff_rows)
        staff_session.commit()

    for index, (chunk_window_start, chunk_window_end) in enumerate(sync_windows):
        events = extractor.fetch_receivable_events(
            window_start=chunk_window_start,
            window_end=chunk_window_end,
        )
        with Session(app_engine) as session:
            chunk_result = sync_receivable_ledger(
                session,
                events,
                snapshot_date=snapshot_date if index + 1 == len(sync_windows) else None,
                employee_counterparty_refs=resolved_employee_refs,
                fired_manager_refs=fired_manager_refs,
            )
            session.commit()

        aggregate_result["processed"] += chunk_result["processed"]
        aggregate_result["inserted"] += chunk_result["inserted"]
        aggregate_result["updated"] += chunk_result["updated"]
        aggregate_result["existing"] += chunk_result["existing"]
        aggregate_result["assignments"] = chunk_result["assignments"]
        aggregate_result["snapshots"] = chunk_result["snapshots"]
        aggregate_result["cases"] = chunk_result["cases"]
        aggregate_result["case_segments"] = chunk_result["case_segments"]
        if index == 0:
            aggregate_result["reset"] = chunk_result["reset"]

    aggregate_result["staff_members"] = staff_result
    aggregate_result["fetched_events"] = aggregate_result["processed"]
    aggregate_result["staff_member_payload_count"] = len(resolved_staff_rows)
    aggregate_result["employee_counterparty_ref_count"] = len(resolved_employee_refs)
    aggregate_result["sync_window_count"] = len(sync_windows)
    return aggregate_result
