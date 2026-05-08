from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import ReceivableBalanceSnapshot, ReceivableCase, StaffingSnapshot
from app.services.receivables import CASE_ADJUSTMENT_CANDIDATE

RULE_RECEIVABLE_NEW_DAILY = "receivable_new_daily"
RULE_RECEIVABLE_OVERDUE = "receivable_overdue"
RULE_RECEIVABLE_EMPLOYEE = "receivable_employee"
RULE_RECEIVABLE_FIRED_MANAGER = "receivable_fired_manager"
RULE_RECEIVABLE_ADJUSTMENT = "receivable_adjustment_candidate"
RULE_RECEIVABLE_ADJUSTMENT_LARGE = "receivable_adjustment_candidate_large"
RULE_STAFFING_DEFICIT = "staffing_shift_deficit"
LARGE_ADJUSTMENT_THRESHOLD = Decimal("10000")


def _deadline(anchor_date: date, *, days_offset: int, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(anchor_date + timedelta(days=days_offset), time(hour, minute))


def _severity_from_balance(balance: Decimal) -> str:
    if balance >= Decimal("10000"):
        return "critical"
    if balance >= Decimal("3000"):
        return "high"
    return "warning"


def _build_receivable_references(case: ReceivableCase) -> list[dict[str, Any]]:
    refs = []
    if case.current_manager_ref or case.current_manager_name:
        refs.append(
            {
                "kind": "current_manager",
                "current_manager_ref": case.current_manager_ref,
                "current_manager_name": case.current_manager_name,
            }
        )
    if case.origin_document_ref:
        refs.append(
            {
                "kind": "origin_document",
                "document_ref": case.origin_document_ref,
                "document_number": case.origin_document_number,
                "document_date": (
                    case.origin_document_date.isoformat() if case.origin_document_date else None
                ),
            }
        )
    for item in case.chain_documents or []:
        refs.append({"kind": "chain_document", **item})
    return refs


def _build_receivable_task(
    *,
    case: ReceivableCase,
    rule_code: str,
    reason: str,
    severity: str,
    owner_code: str,
    as_of: date,
    reaction_deadline_at: datetime,
    due_at: datetime,
    watcher_codes: list[str] | None = None,
    created_by_code: str | None = None,
    dedupe_key: str | None = None,
    suppress_default_observers: bool = False,
    allow_assignee_change_deadline: bool = False,
) -> dict[str, Any]:
    return {
        "rule_code": rule_code,
        "source_type": "receivable_case",
        "entity_ref": case.counterparty_ref,
        "entity_name": case.counterparty_name,
        "severity": severity,
        "owner_code": owner_code,
        "watcher_codes": watcher_codes or [],
        "created_by_code": created_by_code,
        "suppress_default_observers": suppress_default_observers,
        "allow_assignee_change_deadline": allow_assignee_change_deadline,
        "title": f"Дебиторка: {reason} — {case.counterparty_name or case.counterparty_ref}",
        "summary": reason,
        "reaction_deadline_at": reaction_deadline_at,
        "due_at": due_at,
        "dedupe_key": dedupe_key or f"{rule_code}|{as_of.isoformat()}|{case.counterparty_ref}",
        "tags": ["management", "receivables", rule_code],
        "metrics": {
            "current_balance": case.current_balance,
            "aged_bucket": case.aged_bucket,
            "activity_segment": case.activity_segment,
            "due_date": case.due_date,
            "overdue_days": case.overdue_days,
            "payment_term_source": case.payment_term_source,
            "shipment_ban": case.shipment_ban,
        },
        "references": _build_receivable_references(case),
    }


def _receivable_case_from_snapshot(
    snapshot: ReceivableBalanceSnapshot, *, fallback_segment: str = "overdue"
) -> Any:
    return SimpleNamespace(
        snapshot_date=snapshot.snapshot_date,
        segment=fallback_segment,
        owner_type="sales_manager",
        recommendation="Проверить просрочку и согласованный срок оплаты.",
        counterparty_ref=snapshot.counterparty_ref,
        counterparty_name=snapshot.counterparty_name,
        current_balance=snapshot.current_balance,
        aged_bucket=snapshot.aged_bucket,
        activity_segment=snapshot.activity_segment,
        origin_document_ref=snapshot.origin_document_ref,
        origin_document_number=snapshot.origin_document_number,
        origin_document_date=snapshot.origin_document_date,
        origin_manager_ref=snapshot.origin_manager_ref,
        origin_manager_name=snapshot.origin_manager_name,
        current_manager_ref=snapshot.current_manager_ref,
        current_manager_name=snapshot.current_manager_name,
        planned_payment_date=snapshot.planned_payment_date,
        credit_depth_days=snapshot.credit_depth_days,
        shipment_ban=snapshot.shipment_ban,
        payment_term_source=snapshot.payment_term_source,
        due_date=snapshot.due_date,
        overdue_days=snapshot.overdue_days,
        is_overdue=snapshot.is_overdue,
        chain_documents=[],
    )


def _build_staffing_task(
    *,
    snapshot: StaffingSnapshot,
    as_of: date,
) -> dict[str, Any]:
    severity = "critical" if snapshot.criticality == "critical" else "warning"
    watcher_codes = ["hr"] if snapshot.criticality == "critical" else []
    return {
        "rule_code": RULE_STAFFING_DEFICIT,
        "source_type": "staffing_snapshot",
        "entity_ref": snapshot.store_ref,
        "entity_name": snapshot.store_name,
        "severity": severity,
        "owner_code": "retail_supervisor",
        "watcher_codes": watcher_codes,
        "title": f"Некомплект смены — {snapshot.store_name or snapshot.store_ref}",
        "summary": (
            f"Смена {snapshot.shift_code}: дефицит {snapshot.deficit_count}, "
            f"подтверждено {snapshot.confirmed_count} из {snapshot.planned_count}."
        ),
        "reaction_deadline_at": _deadline(as_of, days_offset=0, hour=9, minute=30),
        "due_at": _deadline(as_of, days_offset=0, hour=12, minute=0),
        "dedupe_key": (
            f"{RULE_STAFFING_DEFICIT}|{as_of.isoformat()}|"
            f"{snapshot.store_ref}|{snapshot.shift_code}"
        ),
        "tags": ["management", "staffing", RULE_STAFFING_DEFICIT],
        "metrics": {
            "planned_count": snapshot.planned_count,
            "assigned_count": snapshot.assigned_count,
            "confirmed_count": snapshot.confirmed_count,
            "no_show_count": snapshot.no_show_count,
            "deficit_count": snapshot.deficit_count,
            "fill_rate": snapshot.fill_rate,
        },
        "references": [
            {
                "kind": "staffing_snapshot",
                "snapshot_date": snapshot.snapshot_date.isoformat(),
                "shift_code": snapshot.shift_code,
                "store_ref": snapshot.store_ref,
            }
        ],
    }


def build_management_task_payloads(
    session: Session,
    *,
    as_of: date,
) -> list[dict[str, Any]]:
    settings = get_settings()
    cases = (
        session.execute(
            select(ReceivableCase)
            .where(ReceivableCase.snapshot_date == as_of)
            .order_by(ReceivableCase.current_balance.desc(), ReceivableCase.counterparty_ref)
        )
        .scalars()
        .all()
    )
    snapshots = (
        session.execute(
            select(ReceivableBalanceSnapshot).where(
                ReceivableBalanceSnapshot.snapshot_date == as_of,
                ReceivableBalanceSnapshot.is_overdue.is_(True),
            )
        )
        .scalars()
        .all()
    )
    staffing = (
        session.execute(
            select(StaffingSnapshot).where(
                StaffingSnapshot.snapshot_date == as_of,
                StaffingSnapshot.deficit_count > 0,
            )
        )
        .scalars()
        .all()
    )

    tasks: list[dict[str, Any]] = []

    case_by_counterparty = {item.counterparty_ref: item for item in cases}
    if settings.receivable_task_payloads_enabled:
        for snapshot in snapshots:
            case = case_by_counterparty.get(
                snapshot.counterparty_ref
            ) or _receivable_case_from_snapshot(snapshot)
            tasks.append(
                _build_receivable_task(
                    case=case,
                    rule_code=RULE_RECEIVABLE_OVERDUE,
                    reason=(
                        "Долг просрочен относительно согласованного срока оплаты."
                        if case.payment_term_source == "planned_payment_date"
                        else "Долг просрочен относительно глубины кредита."
                    ),
                    severity=_severity_from_balance(case.current_balance),
                    owner_code="sales_manager",
                    as_of=as_of,
                    reaction_deadline_at=_deadline(as_of, days_offset=0, hour=12),
                    due_at=_deadline(as_of, days_offset=2, hour=18),
                )
            )

        for case in cases:
            if (
                case.segment == CASE_ADJUSTMENT_CANDIDATE
                and case.current_balance >= LARGE_ADJUSTMENT_THRESHOLD
            ):
                tasks.append(
                    _build_receivable_task(
                        case=case,
                        rule_code=RULE_RECEIVABLE_ADJUSTMENT_LARGE,
                        reason=(
                            "Крупный кейс на корректировку: попытаться взыскать, "
                            "при невозможности подготовить списание."
                        ),
                        severity="critical",
                        owner_code="retail_network_head",
                        as_of=as_of,
                        reaction_deadline_at=_deadline(as_of, days_offset=0, hour=15),
                        due_at=_deadline(as_of, days_offset=14, hour=18),
                        watcher_codes=["ceo"],
                        created_by_code="cfo",
                        dedupe_key=f"{RULE_RECEIVABLE_ADJUSTMENT_LARGE}|{case.counterparty_ref}",
                        allow_assignee_change_deadline=True,
                    )
                )

    for snapshot in staffing:
        tasks.append(_build_staffing_task(snapshot=snapshot, as_of=as_of))

    return sorted(tasks, key=lambda item: (item["severity"], item["title"]), reverse=True)
