from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.receivable_balance_snapshot import ReceivableBalanceSnapshot
from app.models.receivable_ledger_event import ReceivableLedgerEvent
from app.services.receivable_decision_portrait import (
    PAYMENT_BEHAVIOR_LABELS_RU,
    PaymentFormMetrics,
    ProfitabilityWindowMetrics,
    _json_ready,
    _money,
    _safe_daily_average,
    build_advisor_recommendation,
    build_credit_policy_metrics,
    build_payment_form_metrics,
    build_payment_metrics,
    build_sales_metrics,
    classify_payment_behavior,
    compute_trend_coefficient,
)
from app.services.receivables import EVENT_PAYMENT, EVENT_RETURN, EVENT_SALE, EVENT_SETTLEMENT

QUERY_CHUNK_SIZE = 900


@dataclass(frozen=True)
class ReceivableCreditProfile:
    snapshot_date: date
    counterparty_ref: str
    counterparty_code: str | None
    counterparty_name: str | None
    department_name: str | None
    manager_name: str | None
    current_balance: Decimal
    credit_depth_days: int | None
    shipment_ban: bool | None
    last_sale_at: date | None
    last_payment_at: date | None
    last_activity_at: date | None
    activity_reason: str
    sales_90: Decimal
    payment_total_90: Decimal
    payment_behavior_group: str
    payment_behavior_label: str
    payment_form: PaymentFormMetrics
    profitability: ProfitabilityWindowMetrics
    credit_discipline_grade: str
    credit_discipline_coefficient: Decimal
    avg_monthly_sales_90: Decimal
    recommended_credit_limit: Decimal
    over_limit_amount: Decimal
    recommended_first_payment_amount: Decimal
    recommended_first_payment_pct: Decimal
    recommended_decision: str
    source_status: str
    source_notes: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return _json_ready(asdict(self))


def build_receivable_credit_profiles(
    session: Session,
    *,
    snapshot_date: date,
    counterparty_refs: Sequence[str],
    active_window_days: int = 365,
    limit: int | None = None,
    profitability_by_ref: Mapping[str, ProfitabilityWindowMetrics] | None = None,
    payment_form_by_ref: Mapping[str, PaymentFormMetrics] | None = None,
) -> list[ReceivableCreditProfile]:
    refs = sorted({ref for ref in counterparty_refs if ref})
    if not refs:
        return []
    active_refs = _load_active_refs(
        session,
        snapshot_date=snapshot_date,
        counterparty_refs=refs,
        active_window_days=active_window_days,
    )
    if not active_refs:
        return []
    if limit is not None:
        active_refs = active_refs[:limit]
    snapshots = _load_snapshots_by_ref(
        session,
        snapshot_date=snapshot_date,
        counterparty_refs=active_refs,
    )
    events_by_ref = _load_events_by_ref(
        session,
        snapshot_date=snapshot_date,
        counterparty_refs=active_refs,
        active_window_days=active_window_days,
    )
    profitability_by_ref = profitability_by_ref or {}
    payment_form_by_ref = payment_form_by_ref or {}
    profiles = [
        _build_credit_profile(
            snapshot_date=snapshot_date,
            counterparty_ref=ref,
            snapshot=snapshots.get(ref),
            events=events_by_ref.get(ref, ()),
            profitability=profitability_by_ref.get(ref) or profitability_by_ref.get(ref.upper()),
            payment_form=payment_form_by_ref.get(ref) or payment_form_by_ref.get(ref.upper()),
        )
        for ref in active_refs
    ]
    return sorted(
        profiles,
        key=lambda item: (
            _grade_sort_key(item.credit_discipline_grade),
            -item.current_balance,
            -(item.sales_90 or Decimal("0")),
            item.counterparty_name or item.counterparty_ref,
        ),
    )


def build_credit_profile_summary(
    profiles: Sequence[ReceivableCreditProfile],
) -> dict[str, object]:
    grade_counts: dict[str, int] = defaultdict(int)
    form_counts: dict[str, int] = defaultdict(int)
    total_balance = Decimal("0.00")
    total_limit = Decimal("0.00")
    total_over_limit = Decimal("0.00")
    total_first_payment = Decimal("0.00")
    partial_count = 0
    for profile in profiles:
        grade_counts[profile.credit_discipline_grade] += 1
        form_counts[profile.payment_form.payment_form_primary] += 1
        total_balance += profile.current_balance
        total_limit += profile.recommended_credit_limit
        total_over_limit += profile.over_limit_amount
        total_first_payment += profile.recommended_first_payment_amount
        if profile.source_status != "ready":
            partial_count += 1
    return {
        "items": len(profiles),
        "partial_source_items": partial_count,
        "total_balance": str(_money(total_balance)),
        "total_recommended_credit_limit": str(_money(total_limit)),
        "total_over_limit_amount": str(_money(total_over_limit)),
        "total_recommended_first_payment": str(_money(total_first_payment)),
        "credit_discipline_counts": dict(sorted(grade_counts.items())),
        "payment_form_counts": dict(sorted(form_counts.items())),
    }


def _build_credit_profile(
    *,
    snapshot_date: date,
    counterparty_ref: str,
    snapshot: ReceivableBalanceSnapshot | None,
    events: Sequence[ReceivableLedgerEvent],
    profitability: ProfitabilityWindowMetrics | None,
    payment_form: PaymentFormMetrics | None,
) -> ReceivableCreditProfile:
    sales = build_sales_metrics(events, snapshot_date=snapshot_date)
    current_balance = _money(snapshot.current_balance) if snapshot else Decimal("0.00")
    payments = build_payment_metrics(
        events,
        snapshot_date=snapshot_date,
        current_balance=current_balance,
        sales_90=sales.sales_90,
    )
    profitability = profitability or ProfitabilityWindowMetrics()
    if profitability.source_status == "ready":
        sales_30 = (
            profitability.revenue_30 if profitability.revenue_30 is not None else sales.sales_30
        )
        sales_60 = (
            profitability.revenue_60 if profitability.revenue_60 is not None else sales.sales_60
        )
        sales_90 = (
            profitability.revenue_90 if profitability.revenue_90 is not None else sales.sales_90
        )
        sales = replace(
            sales,
            sales_30=sales_30,
            sales_60=sales_60,
            sales_90=sales_90,
            avg_sales_30=_safe_daily_average(sales_30, 30),
            avg_sales_60=_safe_daily_average(sales_60, 60),
            avg_sales_90=_safe_daily_average(sales_90, 90),
            trend_coefficient=compute_trend_coefficient(sales_30=sales_30, sales_90=sales_90),
            defect_return_amount_90=profitability.defect_return_amount_90,
            return_filter_status="ready",
        )
        payments = build_payment_metrics(
            events,
            snapshot_date=snapshot_date,
            current_balance=current_balance,
            sales_90=sales.sales_90,
        )
    payment_form = payment_form or build_payment_form_metrics(events, snapshot_date=snapshot_date)
    behavior_group = classify_payment_behavior(
        current_balance=current_balance,
        overdue_days=snapshot.overdue_days if snapshot else None,
        sales=sales,
        payments=payments,
    )
    credit_policy = build_credit_policy_metrics(
        behavior_group=behavior_group,
        current_balance=current_balance,
        overdue_days=snapshot.overdue_days if snapshot else None,
        sales=sales,
        payments=payments,
        payment_form=payment_form,
    )
    advisor = build_advisor_recommendation(
        behavior_group=behavior_group,
        current_balance=current_balance,
        overdue_days=snapshot.overdue_days if snapshot else None,
        sales=sales,
        payments=payments,
        credit_policy=credit_policy,
    )
    latest_event = _latest_event(events)
    last_sale_at = _latest_event_date(events, event_types={EVENT_SALE})
    last_payment_at = _latest_event_date(events, event_types={EVENT_PAYMENT, EVENT_SETTLEMENT})
    source_notes = _profile_source_notes(
        profitability=profitability,
        payment_form=payment_form,
    )
    return ReceivableCreditProfile(
        snapshot_date=snapshot_date,
        counterparty_ref=counterparty_ref,
        counterparty_code=snapshot.counterparty_code if snapshot else None,
        counterparty_name=(
            snapshot.counterparty_name
            if snapshot and snapshot.counterparty_name
            else latest_event.counterparty_name if latest_event else counterparty_ref
        ),
        department_name=(
            snapshot.department_name
            if snapshot and snapshot.department_name
            else latest_event.store_name if latest_event else None
        ),
        manager_name=(
            (snapshot.current_manager_name or snapshot.origin_manager_name)
            if snapshot
            else latest_event.manager_name if latest_event else None
        ),
        current_balance=current_balance,
        credit_depth_days=snapshot.credit_depth_days if snapshot else None,
        shipment_ban=snapshot.shipment_ban if snapshot else None,
        last_sale_at=last_sale_at,
        last_payment_at=last_payment_at,
        last_activity_at=_latest_event_date(events),
        activity_reason=_activity_reason(
            current_balance=current_balance,
            sales_90=sales.sales_90,
            payment_total_90=payments.payment_total_90,
            events=events,
        ),
        sales_90=sales.sales_90,
        payment_total_90=payments.payment_total_90,
        payment_behavior_group=behavior_group,
        payment_behavior_label=PAYMENT_BEHAVIOR_LABELS_RU[behavior_group],
        payment_form=payment_form,
        profitability=profitability,
        credit_discipline_grade=credit_policy.credit_discipline_grade,
        credit_discipline_coefficient=credit_policy.credit_discipline_coefficient,
        avg_monthly_sales_90=credit_policy.avg_monthly_sales_90,
        recommended_credit_limit=credit_policy.recommended_credit_limit,
        over_limit_amount=credit_policy.over_limit_amount,
        recommended_first_payment_amount=credit_policy.recommended_first_payment_amount,
        recommended_first_payment_pct=credit_policy.recommended_first_payment_pct,
        recommended_decision=advisor.recommended_decision_label,
        source_status="partial" if source_notes else "ready",
        source_notes=tuple(source_notes),
    )


def _load_active_refs(
    session: Session,
    *,
    snapshot_date: date,
    counterparty_refs: Sequence[str],
    active_window_days: int,
) -> list[str]:
    active_refs: set[str] = set()
    start_at = datetime.combine(
        snapshot_date - timedelta(days=active_window_days - 1),
        time.min,
    )
    end_at = datetime.combine(snapshot_date + timedelta(days=1), time.min)
    for chunk in _chunks(counterparty_refs, QUERY_CHUNK_SIZE):
        event_query = (
            select(ReceivableLedgerEvent.counterparty_ref)
            .where(
                ReceivableLedgerEvent.counterparty_ref.in_(chunk),
                ReceivableLedgerEvent.external_document_date >= start_at,
                ReceivableLedgerEvent.external_document_date < end_at,
                ReceivableLedgerEvent.event_type.in_(
                    [EVENT_SALE, EVENT_PAYMENT, EVENT_RETURN, EVENT_SETTLEMENT]
                ),
            )
            .distinct()
        )
        active_refs.update(session.execute(event_query).scalars().all())
        debt_query = select(ReceivableBalanceSnapshot.counterparty_ref).where(
            ReceivableBalanceSnapshot.snapshot_date == snapshot_date,
            ReceivableBalanceSnapshot.counterparty_ref.in_(chunk),
            ReceivableBalanceSnapshot.current_balance > 0,
        )
        active_refs.update(session.execute(debt_query).scalars().all())
    return sorted(active_refs)


def _load_snapshots_by_ref(
    session: Session,
    *,
    snapshot_date: date,
    counterparty_refs: Sequence[str],
) -> dict[str, ReceivableBalanceSnapshot]:
    snapshots: dict[str, ReceivableBalanceSnapshot] = {}
    for chunk in _chunks(counterparty_refs, QUERY_CHUNK_SIZE):
        query = select(ReceivableBalanceSnapshot).where(
            ReceivableBalanceSnapshot.snapshot_date == snapshot_date,
            ReceivableBalanceSnapshot.counterparty_ref.in_(chunk),
        )
        for item in session.execute(query).scalars():
            snapshots[item.counterparty_ref] = item
    return snapshots


def _load_events_by_ref(
    session: Session,
    *,
    snapshot_date: date,
    counterparty_refs: Sequence[str],
    active_window_days: int,
) -> dict[str, list[ReceivableLedgerEvent]]:
    start_at = datetime.combine(
        snapshot_date - timedelta(days=active_window_days - 1),
        time.min,
    )
    end_at = datetime.combine(snapshot_date + timedelta(days=1), time.min)
    events_by_ref: dict[str, list[ReceivableLedgerEvent]] = defaultdict(list)
    for chunk in _chunks(counterparty_refs, QUERY_CHUNK_SIZE):
        query = (
            select(ReceivableLedgerEvent)
            .where(
                ReceivableLedgerEvent.counterparty_ref.in_(chunk),
                ReceivableLedgerEvent.external_document_date >= start_at,
                ReceivableLedgerEvent.external_document_date < end_at,
            )
            .order_by(
                ReceivableLedgerEvent.counterparty_ref,
                ReceivableLedgerEvent.external_document_date,
                ReceivableLedgerEvent.id,
            )
        )
        for event in session.execute(query).scalars():
            events_by_ref[event.counterparty_ref].append(event)
    return events_by_ref


def _latest_event(events: Sequence[ReceivableLedgerEvent]) -> ReceivableLedgerEvent | None:
    return max(events, key=lambda event: event.external_document_date, default=None)


def _latest_event_date(
    events: Sequence[ReceivableLedgerEvent],
    *,
    event_types: set[str] | None = None,
) -> date | None:
    filtered = [
        event.external_document_date.date()
        for event in events
        if event_types is None or event.event_type in event_types
    ]
    return max(filtered) if filtered else None


def _activity_reason(
    *,
    current_balance: Decimal,
    sales_90: Decimal,
    payment_total_90: Decimal,
    events: Sequence[ReceivableLedgerEvent],
) -> str:
    if current_balance > 0:
        return "current_debt"
    if sales_90 > 0 or payment_total_90 > 0:
        return "active_90"
    if events:
        return "active_365"
    return "inactive"


def _profile_source_notes(
    *,
    profitability: ProfitabilityWindowMetrics,
    payment_form: PaymentFormMetrics,
) -> list[str]:
    notes: list[str] = []
    if profitability.source_status != "ready":
        notes.append(profitability.source_note)
    if payment_form.source_status != "ready":
        notes.append(payment_form.source_note)
    return notes


def _grade_sort_key(grade: str) -> int:
    return {"E": 0, "D": 1, "C": 2, "B": 3, "A": 4}.get(grade, 5)


def _chunks(values: Sequence[str], size: int):
    for index in range(0, len(values), size):
        yield values[index : index + size]
