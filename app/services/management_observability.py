from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import (
    ReceivableBalanceSnapshot,
    ReceivableCase,
    ReceivableLedgerEvent,
    ReceivableReconciliationSnapshot,
    StaffingSnapshot,
    StoreShiftFact,
    StoreShiftPlan,
)
from app.services.management_rules import build_management_task_payloads

REQUIRED_RECEIVABLE_OPENING_BALANCE_DATE = date(2025, 1, 1)
RECEIVABLE_AMOUNT_TOLERANCE = 0.01


def _latest_snapshot_date(session: Session, model, column) -> date | None:
    return session.execute(select(func.max(column)).select_from(model)).scalar_one_or_none()


def _snapshot_date_for_request(
    session: Session, model, column, requested_date: date
) -> date | None:
    requested_count = _count_for_date(session, model, column, requested_date)
    if requested_count > 0:
        return requested_date
    return _latest_snapshot_date(session, model, column)


def _count_for_date(session: Session, model, column, snapshot_date: date | None) -> int:
    if snapshot_date is None:
        return 0
    return int(
        session.execute(
            select(func.count()).select_from(model).where(column == snapshot_date)
        ).scalar_one()
        or 0
    )


def _count_and_sum_for_date(session: Session, model, date_column, amount_column, snapshot_date):
    if snapshot_date is None:
        return 0, 0.0
    count_value, total_value = session.execute(
        select(func.count(), func.coalesce(func.sum(amount_column), 0))
        .select_from(model)
        .where(date_column == snapshot_date)
    ).one()
    return int(count_value or 0), float(total_value or 0)


def _case_count_and_sum_for_segment(
    session: Session,
    *,
    snapshot_date: date | None,
    segment: str,
) -> tuple[int, float]:
    if snapshot_date is None:
        return 0, 0.0
    count_value, total_value = session.execute(
        select(func.count(), func.coalesce(func.sum(ReceivableCase.current_balance), 0))
        .select_from(ReceivableCase)
        .where(
            ReceivableCase.snapshot_date == snapshot_date,
            ReceivableCase.segment == segment,
        )
    ).one()
    return int(count_value or 0), float(total_value or 0)


def _synthetic_receivable_ref_count(session: Session, *, snapshot_date: date | None) -> int:
    if snapshot_date is None:
        return 0
    return int(
        session.execute(
            select(func.count())
            .select_from(ReceivableBalanceSnapshot)
            .where(
                ReceivableBalanceSnapshot.snapshot_date == snapshot_date,
                ReceivableBalanceSnapshot.counterparty_ref.like("synthetic:%"),
            )
        ).scalar_one()
        or 0
    )


def _freshness_status(
    *,
    requested_date: date,
    latest_date: date | None,
    max_lag_days: int,
) -> tuple[str, int | None]:
    if latest_date is None:
        return "missing", None
    lag_days = max((requested_date - latest_date).days, 0)
    if lag_days <= max_lag_days:
        return "fresh", lag_days
    return "stale", lag_days


def _date_value(value: Any) -> date | None:
    if value is None:
        return None
    date_method = getattr(value, "date", None)
    if callable(date_method):
        return date_method()
    if isinstance(value, date):
        return value
    return None


def _receivable_ledger_authoritative_status(session: Session) -> dict[str, Any]:
    ledger_row_count = int(
        session.execute(select(func.count()).select_from(ReceivableLedgerEvent)).scalar_one() or 0
    )
    min_event_at = session.execute(
        select(func.min(ReceivableLedgerEvent.external_document_date))
    ).scalar_one_or_none()
    max_event_at = session.execute(
        select(func.max(ReceivableLedgerEvent.external_document_date))
    ).scalar_one_or_none()
    opening_import_row_count = int(
        session.execute(
            select(func.count())
            .select_from(ReceivableLedgerEvent)
            .where(
                or_(
                    ReceivableLedgerEvent.source_layer == "opening_import_1c",
                    ReceivableLedgerEvent.source == "onec_opening_import",
                )
            )
        ).scalar_one()
        or 0
    )
    min_event_date = _date_value(min_event_at)
    max_event_date = _date_value(max_event_at)
    is_ready = (
        ledger_row_count > 0
        and opening_import_row_count > 0
        and min_event_date == REQUIRED_RECEIVABLE_OPENING_BALANCE_DATE
    )
    if is_ready:
        source_status = "ready"
        note = ""
    elif ledger_row_count == 0:
        source_status = "empty"
        note = "receivable_ledger_event пустой"
    else:
        source_status = "partial"
        note = (
            "ledger не подтвержден как authoritative: нужен opening seed "
            f"{REQUIRED_RECEIVABLE_OPENING_BALANCE_DATE.isoformat()} и движения 1С"
        )
    return {
        "ledger_source_status": source_status,
        "ledger_authoritative_ready": is_ready,
        "ledger_row_count": ledger_row_count,
        "ledger_min_event_date": min_event_date,
        "ledger_max_event_date": max_event_date,
        "opening_import_event_count": opening_import_row_count,
        "required_opening_balance_date": REQUIRED_RECEIVABLE_OPENING_BALANCE_DATE,
        "ledger_note": note,
    }


def build_management_health(session: Session, *, as_of: date) -> dict[str, Any]:
    settings = get_settings()

    receivables_snapshot_date = _snapshot_date_for_request(
        session,
        ReceivableBalanceSnapshot,
        ReceivableBalanceSnapshot.snapshot_date,
        as_of,
    )
    receivables_case_date = _snapshot_date_for_request(
        session,
        ReceivableCase,
        ReceivableCase.snapshot_date,
        as_of,
    )
    receivables_latest_date = (
        min(
            [
                item
                for item in [receivables_snapshot_date, receivables_case_date]
                if item is not None
            ]
        )
        if any([receivables_snapshot_date, receivables_case_date])
        else None
    )
    receivables_freshness, receivables_lag = _freshness_status(
        requested_date=as_of,
        latest_date=receivables_latest_date,
        max_lag_days=settings.management_receivables_max_lag_days,
    )
    receivables_base_source_status = (
        "ready"
        if receivables_snapshot_date
        and receivables_case_date
        and receivables_snapshot_date == receivables_case_date
        else "partial" if receivables_snapshot_date or receivables_case_date else "empty"
    )
    receivables_source_status = receivables_base_source_status
    receivables_ledger_metrics = _receivable_ledger_authoritative_status(session)
    snapshot_count, snapshot_total = _count_and_sum_for_date(
        session,
        ReceivableBalanceSnapshot,
        ReceivableBalanceSnapshot.snapshot_date,
        ReceivableBalanceSnapshot.current_balance,
        receivables_snapshot_date,
    )
    reconciliation_date = _snapshot_date_for_request(
        session,
        ReceivableReconciliationSnapshot,
        ReceivableReconciliationSnapshot.snapshot_date,
        as_of,
    )
    reconciliation_count, reconciliation_total = _count_and_sum_for_date(
        session,
        ReceivableReconciliationSnapshot,
        ReceivableReconciliationSnapshot.snapshot_date,
        ReceivableReconciliationSnapshot.signed_balance,
        reconciliation_date,
    )
    case_count = _count_for_date(
        session,
        ReceivableCase,
        ReceivableCase.snapshot_date,
        receivables_case_date,
    )
    buyer_case_count, buyer_case_total = _case_count_and_sum_for_segment(
        session,
        snapshot_date=receivables_case_date,
        segment="buyers",
    )
    synthetic_ref_count = _synthetic_receivable_ref_count(
        session,
        snapshot_date=receivables_snapshot_date,
    )
    snapshot_reconciliation_match = (
        receivables_snapshot_date is not None
        and reconciliation_date is not None
        and receivables_snapshot_date == reconciliation_date
        and snapshot_count == reconciliation_count
        and abs(snapshot_total - reconciliation_total) <= RECEIVABLE_AMOUNT_TOLERANCE
    )
    receivables_quality_issues: list[str] = []
    if receivables_snapshot_date and synthetic_ref_count > 0:
        receivables_quality_issues.append("synthetic_counterparty_refs")
    if receivables_snapshot_date and not snapshot_reconciliation_match:
        receivables_quality_issues.append("snapshot_reconciliation_mismatch")
    if receivables_quality_issues:
        receivables_source_status = "partial"

    staffing_snapshot_date = _latest_snapshot_date(
        session, StaffingSnapshot, StaffingSnapshot.snapshot_date
    )
    staffing_plan_date = _latest_snapshot_date(session, StoreShiftPlan, StoreShiftPlan.shift_date)
    staffing_fact_date = _latest_snapshot_date(session, StoreShiftFact, StoreShiftFact.shift_date)
    staffing_freshness, staffing_lag = _freshness_status(
        requested_date=as_of,
        latest_date=staffing_snapshot_date,
        max_lag_days=settings.management_staffing_max_lag_days,
    )
    staffing_source_status = (
        "ready"
        if staffing_snapshot_date and staffing_plan_date and staffing_fact_date
        else (
            "partial"
            if staffing_snapshot_date or staffing_plan_date or staffing_fact_date
            else "empty"
        )
    )

    latest_task_date_candidates = [
        item for item in [receivables_latest_date, staffing_snapshot_date] if item
    ]
    task_latest_date = min(latest_task_date_candidates) if latest_task_date_candidates else None
    task_freshness, task_lag = _freshness_status(
        requested_date=as_of,
        latest_date=task_latest_date,
        max_lag_days=settings.management_task_payloads_max_lag_days,
    )
    task_source_status = (
        "ready"
        if receivables_source_status == "ready" and staffing_source_status == "ready"
        else (
            "partial"
            if receivables_source_status != "empty" or staffing_source_status != "empty"
            else "empty"
        )
    )
    task_payload_count = len(
        build_management_task_payloads(session, as_of=task_latest_date or as_of)
    )

    components = [
        {
            "component": "receivables",
            "freshness_status": receivables_freshness,
            "source_status": receivables_source_status,
            "latest_snapshot_date": receivables_latest_date,
            "requested_date": as_of,
            "lag_days": receivables_lag,
            "metrics": {
                "latest_balance_snapshot_date": receivables_snapshot_date,
                "latest_reconciliation_snapshot_date": reconciliation_date,
                "latest_case_date": receivables_case_date,
                "snapshot_count": snapshot_count,
                "snapshot_signed_total": snapshot_total,
                "reconciliation_count": reconciliation_count,
                "reconciliation_signed_total": reconciliation_total,
                "snapshot_reconciliation_match": snapshot_reconciliation_match,
                "synthetic_ref_count": synthetic_ref_count,
                "case_count": case_count,
                "buyer_case_count": buyer_case_count,
                "buyer_case_total_balance": buyer_case_total,
                "quality_issues": receivables_quality_issues,
                "authoritative_balance_source": "receivable_balance_snapshot",
                "ledger_role": "enrichment",
                **receivables_ledger_metrics,
            },
        },
        {
            "component": "staffing",
            "freshness_status": staffing_freshness,
            "source_status": staffing_source_status,
            "latest_snapshot_date": staffing_snapshot_date,
            "requested_date": as_of,
            "lag_days": staffing_lag,
            "metrics": {
                "latest_plan_date": staffing_plan_date,
                "latest_fact_date": staffing_fact_date,
                "snapshot_count": _count_for_date(
                    session,
                    StaffingSnapshot,
                    StaffingSnapshot.snapshot_date,
                    staffing_snapshot_date,
                ),
            },
        },
        {
            "component": "task_payloads",
            "freshness_status": task_freshness,
            "source_status": task_source_status,
            "latest_snapshot_date": task_latest_date,
            "requested_date": as_of,
            "lag_days": task_lag,
            "metrics": {
                "task_payload_count": task_payload_count,
            },
        },
    ]

    overall_freshness = "fresh"
    if any(item["freshness_status"] == "missing" for item in components):
        overall_freshness = "missing"
    elif any(item["freshness_status"] == "stale" for item in components):
        overall_freshness = "stale"

    overall_source_status = "ready"
    if any(item["source_status"] == "empty" for item in components):
        overall_source_status = "empty"
    elif any(item["source_status"] == "partial" for item in components):
        overall_source_status = "partial"

    overall_status = (
        "ok" if overall_freshness == "fresh" and overall_source_status == "ready" else "degraded"
    )
    return {
        "as_of": as_of,
        "status": overall_status,
        "freshness_status": overall_freshness,
        "source_status": overall_source_status,
        "components": components,
    }
