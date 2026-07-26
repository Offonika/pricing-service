from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from statistics import median
from typing import Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.counterparty_folder_snapshot import CounterpartyFolderSnapshot
from app.models.receivable_balance_snapshot import ReceivableBalanceSnapshot
from app.models.receivable_ledger_event import ReceivableLedgerEvent
from app.services.receivables import EVENT_PAYMENT, EVENT_RETURN, EVENT_SALE, EVENT_SETTLEMENT

WINDOW_30_DAYS = 30
WINDOW_60_DAYS = 60
WINDOW_90_DAYS = 90
MONEY_QUANT = Decimal("0.01")
RATIO_QUANT = Decimal("0.01")
BASE_CREDIT_LIMIT_RATE = Decimal("0.30")
DEFAULT_ONEC_FOLDER_FILTER = "Покупатели"

PAYMENT_BEHAVIOR_LABELS_RU = {
    "no_current_debt": "Нет текущего долга",
    "weekly_batch_payer": "Частые заказы, недельная оплата пачкой",
    "regular_term_payer": "Стабильно платит около срока",
    "partial_but_alive": "Платит частями, долг не разгоняется",
    "growing_debt_second_week": "Долг растет вторую неделю",
    "chronic_non_payer": "Злостный неплательщик",
    "promise_breaker": "Срывает обещания оплаты",
    "silent_no_contact": "Нет платежей и нет свежего движения",
    "dispute_quality": "Спор / брак / проверка суммы",
    "new_no_history": "Новый, мало истории",
}

RECOMMENDED_DECISION_LABELS_RU = {
    "soft_work": "Работать мягко",
    "strict_control": "Жесткий контроль",
    "shipment_stop": "Стоп отгрузка",
    "verify_amount": "Проверить сумму",
    "escalate": "Эскалация",
    "close": "Закрыть",
}

ADVISOR_TONE_LABELS_RU = {
    "partner_soft": "Партнерски и мягко",
    "calm_strict": "Спокойно, но жестко",
    "fact_check": "Сбор фактов без давления",
    "final_warning": "Финальное предупреждение",
}

CREDIT_DISCIPLINE_LABELS_RU = {
    "A": "Отличная дисциплина",
    "B": "Нормальная дисциплина",
    "C": "Осторожно",
    "D": "Риск",
    "E": "Стоп / предоплата",
}

CREDIT_DISCIPLINE_COEFFICIENTS = {
    "A": Decimal("1.20"),
    "B": Decimal("1.00"),
    "C": Decimal("0.70"),
    "D": Decimal("0.40"),
    "E": Decimal("0.00"),
}


@dataclass(frozen=True)
class FolderFilterResult:
    folder_name: str | None
    snapshot_date: date | None
    status: str
    source: str
    counterparty_refs: tuple[str, ...]

    @property
    def applied(self) -> bool:
        return bool(self.folder_name and self.status == "ready")

    def to_dict(self) -> dict[str, object]:
        payload = _json_ready(asdict(self))
        refs = list(self.counterparty_refs)
        payload["counterparty_ref_count"] = len(refs)
        payload["counterparty_refs_sample"] = refs[:10]
        payload.pop("counterparty_refs", None)
        return payload


@dataclass(frozen=True)
class ProfitabilityWindowMetrics:
    revenue_30: Decimal | None = None
    revenue_60: Decimal | None = None
    revenue_90: Decimal | None = None
    cost_of_sales_30: Decimal | None = None
    cost_of_sales_60: Decimal | None = None
    cost_of_sales_90: Decimal | None = None
    gross_profit_30: Decimal | None = None
    gross_profit_60: Decimal | None = None
    gross_profit_90: Decimal | None = None
    gross_margin_pct_90: Decimal | None = None
    profitability_pct_90: Decimal | None = None
    defect_return_amount_30: Decimal | None = None
    defect_return_amount_60: Decimal | None = None
    defect_return_amount_90: Decimal | None = None
    source_status: str = "missing_counterparty_cost"
    source_note: str = (
        "В журнале дебиторки нет себестоимости по контрагенту; нужна отдельная "
        "1С-витрина продаж и себестоимости."
    )


@dataclass(frozen=True)
class SalesWindowMetrics:
    sales_30: Decimal
    sales_60: Decimal
    sales_90: Decimal
    avg_sales_30: Decimal
    avg_sales_60: Decimal
    avg_sales_90: Decimal
    trend_coefficient: Decimal
    sale_order_count_90: int
    active_sale_days_90: int
    return_amount_90: Decimal
    defect_return_amount_90: Decimal | None
    return_filter_status: str


@dataclass(frozen=True)
class PaymentWindowMetrics:
    payment_total_90: Decimal
    payment_count_90: int
    payment_days_90: int
    median_payment_interval_days: Decimal | None
    last_payment_date: date | None
    payment_to_sales_90_pct: Decimal | None
    debt_to_sales_90_ratio: Decimal | None
    last_14_sales: Decimal
    last_14_payments: Decimal


@dataclass(frozen=True)
class PaymentFormMetrics:
    payment_form_primary: str
    cash_share_90: Decimal | None
    bank_share_90: Decimal | None
    source_status: str
    source_note: str


@dataclass(frozen=True)
class CreditPolicyMetrics:
    credit_discipline_grade: str
    credit_discipline_label: str
    credit_discipline_coefficient: Decimal
    avg_monthly_sales_90: Decimal
    base_credit_limit_rate: Decimal
    recommended_credit_limit: Decimal
    over_limit_amount: Decimal
    recommended_first_payment_amount: Decimal
    recommended_first_payment_pct: Decimal
    payment_plan_type: str
    policy_note: str


@dataclass(frozen=True)
class AdvisorRecommendation:
    recommended_decision: str
    recommended_decision_label: str
    recommended_first_payment_pct: Decimal
    recommended_first_payment_amount: Decimal
    recommended_payment_window_days: int
    advisor_tone: str
    advisor_tone_label: str
    advisor_summary: str
    negotiation_goal: str
    talk_track: str
    allowed_concession: str
    forbidden_promises: str
    escalation_trigger: str


@dataclass(frozen=True)
class ReceivableDecisionPortrait:
    snapshot_date: date
    counterparty_ref: str
    counterparty_code: str | None
    counterparty_name: str | None
    department_ref: str | None
    department_name: str | None
    manager_ref: str | None
    manager_name: str | None
    current_balance: Decimal
    overdue_days: int | None
    due_date: date | None
    aged_bucket: str
    activity_segment: str
    sales: SalesWindowMetrics
    profitability: ProfitabilityWindowMetrics
    payments: PaymentWindowMetrics
    payment_form: PaymentFormMetrics
    credit_policy: CreditPolicyMetrics
    payment_behavior_group: str
    payment_behavior_label: str
    advisor: AdvisorRecommendation
    source_status: str
    source_notes: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return _json_ready(asdict(self))


def build_receivable_decision_portraits(
    session: Session,
    *,
    snapshot_date: date,
    limit: int | None = None,
    counterparty_refs: Sequence[str] = (),
    profitability_by_ref: Mapping[str, ProfitabilityWindowMetrics] | None = None,
    payment_form_by_ref: Mapping[str, PaymentFormMetrics] | None = None,
) -> list[ReceivableDecisionPortrait]:
    snapshots = _load_snapshots(
        session,
        snapshot_date=snapshot_date,
        limit=limit,
        counterparty_refs=counterparty_refs,
    )
    if not snapshots:
        return []

    refs = [snapshot.counterparty_ref for snapshot in snapshots]
    events_by_ref = _load_events_by_counterparty(
        session,
        snapshot_date=snapshot_date,
        counterparty_refs=refs,
    )
    profitability_by_ref = profitability_by_ref or {}
    payment_form_by_ref = payment_form_by_ref or {}
    portraits: list[ReceivableDecisionPortrait] = []
    for snapshot in snapshots:
        portraits.append(
            build_receivable_decision_portrait(
                snapshot,
                events=events_by_ref.get(snapshot.counterparty_ref, ()),
                profitability=(
                    profitability_by_ref.get(snapshot.counterparty_ref)
                    or profitability_by_ref.get(snapshot.counterparty_ref.upper())
                ),
                payment_form=(
                    payment_form_by_ref.get(snapshot.counterparty_ref)
                    or payment_form_by_ref.get(snapshot.counterparty_ref.upper())
                ),
            )
        )
    return portraits


def load_counterparty_refs_for_folder(
    session: Session,
    *,
    snapshot_date: date,
    folder_name: str | None = DEFAULT_ONEC_FOLDER_FILTER,
) -> FolderFilterResult:
    if not folder_name:
        return FolderFilterResult(
            folder_name=None,
            snapshot_date=None,
            status="not_requested",
            source="none",
            counterparty_refs=(),
        )

    folder_snapshot_date = (
        session.execute(
            select(CounterpartyFolderSnapshot.snapshot_date)
            .where(CounterpartyFolderSnapshot.snapshot_date <= snapshot_date)
            .order_by(CounterpartyFolderSnapshot.snapshot_date.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )
    if folder_snapshot_date is None:
        return FolderFilterResult(
            folder_name=folder_name,
            snapshot_date=None,
            status="missing_folder_snapshot",
            source="folder_snapshot",
            counterparty_refs=(),
        )

    rows = (
        session.execute(
            select(
                CounterpartyFolderSnapshot.counterparty_ref,
                CounterpartyFolderSnapshot.current_folder_name,
            ).where(CounterpartyFolderSnapshot.snapshot_date == folder_snapshot_date)
        )
        .mappings()
        .all()
    )
    folder_key = _folder_key(folder_name)
    refs = sorted(
        row["counterparty_ref"]
        for row in rows
        if row["counterparty_ref"] and _folder_key(row["current_folder_name"]) == folder_key
    )
    return FolderFilterResult(
        folder_name=folder_name,
        snapshot_date=folder_snapshot_date,
        status="ready",
        source="folder_snapshot",
        counterparty_refs=tuple(refs),
    )


def build_receivable_decision_portrait(
    snapshot: ReceivableBalanceSnapshot,
    *,
    events: Sequence[ReceivableLedgerEvent],
    profitability: ProfitabilityWindowMetrics | None = None,
    payment_form: PaymentFormMetrics | None = None,
) -> ReceivableDecisionPortrait:
    snapshot_date = snapshot.snapshot_date
    current_balance = _money(snapshot.current_balance)
    effective_overdue_days = snapshot.overdue_days if current_balance > 0 else None
    effective_due_date = _to_date(snapshot.due_date) if current_balance > 0 else None
    sales = build_sales_metrics(events, snapshot_date=snapshot_date)
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
            avg_sales_30=_safe_daily_average(sales_30, WINDOW_30_DAYS),
            avg_sales_60=_safe_daily_average(sales_60, WINDOW_60_DAYS),
            avg_sales_90=_safe_daily_average(sales_90, WINDOW_90_DAYS),
            trend_coefficient=compute_trend_coefficient(sales_30=sales_30, sales_90=sales_90),
            defect_return_amount_90=profitability.defect_return_amount_90,
            return_filter_status="ready",
        )
        payments = replace(
            payments,
            payment_to_sales_90_pct=_percent(payments.payment_total_90, sales.sales_90),
            debt_to_sales_90_ratio=(
                _ratio(current_balance / sales.sales_90) if sales.sales_90 > 0 else None
            ),
        )
    behavior_group = classify_payment_behavior(
        current_balance=current_balance,
        overdue_days=effective_overdue_days,
        sales=sales,
        payments=payments,
    )
    payment_form = payment_form or build_payment_form_metrics(events, snapshot_date=snapshot_date)
    credit_policy = build_credit_policy_metrics(
        behavior_group=behavior_group,
        current_balance=current_balance,
        overdue_days=effective_overdue_days,
        sales=sales,
        payments=payments,
        payment_form=payment_form,
    )
    advisor = build_advisor_recommendation(
        behavior_group=behavior_group,
        current_balance=current_balance,
        overdue_days=effective_overdue_days,
        sales=sales,
        payments=payments,
        credit_policy=credit_policy,
    )
    source_notes = _source_notes(profitability=profitability, sales=sales)
    return ReceivableDecisionPortrait(
        snapshot_date=snapshot.snapshot_date,
        counterparty_ref=snapshot.counterparty_ref,
        counterparty_code=snapshot.counterparty_code,
        counterparty_name=snapshot.counterparty_name,
        department_ref=snapshot.department_ref,
        department_name=snapshot.department_name,
        manager_ref=snapshot.current_manager_ref or snapshot.origin_manager_ref,
        manager_name=snapshot.current_manager_name or snapshot.origin_manager_name,
        current_balance=current_balance,
        overdue_days=effective_overdue_days,
        due_date=effective_due_date,
        aged_bucket=snapshot.aged_bucket,
        activity_segment=snapshot.activity_segment,
        sales=sales,
        profitability=profitability,
        payments=payments,
        payment_form=payment_form,
        credit_policy=credit_policy,
        payment_behavior_group=behavior_group,
        payment_behavior_label=PAYMENT_BEHAVIOR_LABELS_RU[behavior_group],
        advisor=advisor,
        source_status="partial" if source_notes else "ready",
        source_notes=tuple(source_notes),
    )


def build_sales_metrics(
    events: Sequence[ReceivableLedgerEvent],
    *,
    snapshot_date: date,
) -> SalesWindowMetrics:
    sales_30 = _sum_sale_events(events, snapshot_date=snapshot_date, days=WINDOW_30_DAYS)
    sales_60 = _sum_sale_events(events, snapshot_date=snapshot_date, days=WINDOW_60_DAYS)
    sales_90 = _sum_sale_events(events, snapshot_date=snapshot_date, days=WINDOW_90_DAYS)
    sale_dates_90 = {
        event.external_document_date.date()
        for event in events
        if _is_within_window(event, snapshot_date=snapshot_date, days=WINDOW_90_DAYS)
        and event.event_type == EVENT_SALE
        and Decimal(event.amount_delta) > 0
    }
    return SalesWindowMetrics(
        sales_30=sales_30,
        sales_60=sales_60,
        sales_90=sales_90,
        avg_sales_30=_safe_daily_average(sales_30, WINDOW_30_DAYS),
        avg_sales_60=_safe_daily_average(sales_60, WINDOW_60_DAYS),
        avg_sales_90=_safe_daily_average(sales_90, WINDOW_90_DAYS),
        trend_coefficient=compute_trend_coefficient(sales_30=sales_30, sales_90=sales_90),
        sale_order_count_90=sum(
            1
            for event in events
            if _is_within_window(event, snapshot_date=snapshot_date, days=WINDOW_90_DAYS)
            and event.event_type == EVENT_SALE
            and Decimal(event.amount_delta) > 0
        ),
        active_sale_days_90=len(sale_dates_90),
        return_amount_90=_sum_return_events(events, snapshot_date=snapshot_date),
        defect_return_amount_90=None,
        return_filter_status="return_reason_missing",
    )


def build_payment_metrics(
    events: Sequence[ReceivableLedgerEvent],
    *,
    snapshot_date: date,
    current_balance: Decimal,
    sales_90: Decimal,
) -> PaymentWindowMetrics:
    payment_events = [
        event
        for event in events
        if _is_within_window(event, snapshot_date=snapshot_date, days=WINDOW_90_DAYS)
        and _is_payment_event(event)
    ]
    payment_dates = sorted({event.external_document_date.date() for event in payment_events})
    payment_total_90 = _money(sum(abs(Decimal(event.amount_delta)) for event in payment_events))
    intervals = [
        (payment_dates[index] - payment_dates[index - 1]).days
        for index in range(1, len(payment_dates))
    ]
    median_interval = _ratio(Decimal(str(median(intervals)))) if intervals else None
    last_payment_date = max(payment_dates) if payment_dates else None
    return PaymentWindowMetrics(
        payment_total_90=payment_total_90,
        payment_count_90=len(payment_events),
        payment_days_90=len(payment_dates),
        median_payment_interval_days=median_interval,
        last_payment_date=last_payment_date,
        payment_to_sales_90_pct=_percent(payment_total_90, sales_90),
        debt_to_sales_90_ratio=_ratio(current_balance / sales_90) if sales_90 > 0 else None,
        last_14_sales=_sum_sale_events(events, snapshot_date=snapshot_date, days=14),
        last_14_payments=_sum_payment_events(events, snapshot_date=snapshot_date, days=14),
    )


def build_payment_form_metrics(
    events: Sequence[ReceivableLedgerEvent],
    *,
    snapshot_date: date,
) -> PaymentFormMetrics:
    payment_total_90 = _sum_payment_events(
        events,
        snapshot_date=snapshot_date,
        days=WINDOW_90_DAYS,
    )
    if payment_total_90 <= 0:
        return PaymentFormMetrics(
            payment_form_primary="unknown",
            cash_share_90=None,
            bank_share_90=None,
            source_status="no_payments_90",
            source_note="За 90 дней нет оплат: форму оплаты определить нельзя.",
        )
    return PaymentFormMetrics(
        payment_form_primary="unknown",
        cash_share_90=None,
        bank_share_90=None,
        source_status="missing_payment_form_source",
        source_note=(
            "В локальном журнале дебиторки форма оплаты нал/безнал не сохранена; нужен "
            "отдельный read-only классификатор 1С по документам оплат."
        ),
    )


def compute_trend_coefficient(*, sales_30: Decimal, sales_90: Decimal) -> Decimal:
    sales_30 = _money(sales_30)
    sales_90 = _money(sales_90)
    if sales_30 <= 0 and sales_90 <= 0:
        return Decimal("1.00")
    if sales_90 <= 0:
        return Decimal("1.20")

    avg_30 = sales_30 / Decimal(WINDOW_30_DAYS)
    avg_90 = sales_90 / Decimal(WINDOW_90_DAYS)
    if avg_90 <= 0:
        return Decimal("1.20")

    coefficient = avg_30 / avg_90
    if coefficient > Decimal("1.20"):
        coefficient = Decimal("1.20")
    if coefficient < Decimal("0.50"):
        coefficient = Decimal("0.50")
    return _ratio(coefficient)


def classify_payment_behavior(
    *,
    current_balance: Decimal,
    overdue_days: int | None,
    sales: SalesWindowMetrics,
    payments: PaymentWindowMetrics,
    has_quality_dispute: bool = False,
    promise_broken: bool = False,
) -> str:
    current_balance = _money(current_balance)
    overdue_days = overdue_days or 0
    if current_balance <= 0:
        return "no_current_debt"
    if has_quality_dispute:
        return "dispute_quality"
    if promise_broken:
        return "promise_breaker"
    if sales.sales_90 <= 0 and payments.payment_count_90 == 0:
        return "new_no_history" if overdue_days < 14 else "silent_no_contact"
    debt_to_sales_ratio = current_balance / sales.sales_90 if sales.sales_90 > 0 else None
    if (
        sales.sales_90 > 0
        and current_balance > sales.sales_90
        and payments.payment_total_90 < current_balance * Decimal("0.30")
        and (overdue_days >= 30 or debt_to_sales_ratio >= Decimal("2"))
    ):
        return "chronic_non_payer"

    recent_gap = payments.last_14_sales - payments.last_14_payments
    if overdue_days >= 7 and recent_gap > max(sales.sales_90 * Decimal("0.08"), Decimal("1000")):
        return "growing_debt_second_week"

    if (
        sales.sale_order_count_90 >= 8
        and payments.payment_days_90 >= 4
        and payments.median_payment_interval_days is not None
        and Decimal("5") <= payments.median_payment_interval_days <= Decimal("10")
        and _covers_sales(payments.payment_total_90, sales.sales_90, Decimal("0.65"))
        and (payments.debt_to_sales_90_ratio is None or payments.debt_to_sales_90_ratio <= 1)
    ):
        return "weekly_batch_payer"

    if (
        payments.payment_days_90 >= 3
        and _covers_sales(payments.payment_total_90, sales.sales_90, Decimal("0.60"))
        and overdue_days < 30
        and (payments.debt_to_sales_90_ratio is None or payments.debt_to_sales_90_ratio <= 1)
    ):
        return "regular_term_payer"

    if payments.payment_total_90 > 0:
        return "partial_but_alive"

    return "silent_no_contact" if overdue_days >= 14 else "new_no_history"


def build_credit_policy_metrics(
    *,
    behavior_group: str,
    current_balance: Decimal,
    overdue_days: int | None,
    sales: SalesWindowMetrics,
    payments: PaymentWindowMetrics,
    payment_form: PaymentFormMetrics,
) -> CreditPolicyMetrics:
    grade = _credit_discipline_grade(
        behavior_group=behavior_group,
        current_balance=current_balance,
        overdue_days=overdue_days,
        sales=sales,
        payments=payments,
        payment_form=payment_form,
    )
    coefficient = CREDIT_DISCIPLINE_COEFFICIENTS[grade]
    avg_monthly_sales_90 = _money(sales.sales_90 / Decimal("3"))
    recommended_credit_limit = _money(avg_monthly_sales_90 * BASE_CREDIT_LIMIT_RATE * coefficient)
    over_limit_amount = _money(
        max(_money(current_balance) - recommended_credit_limit, Decimal("0"))
    )
    monthly_payment_cap = _money(avg_monthly_sales_90 * BASE_CREDIT_LIMIT_RATE)
    if grade == "E":
        first_payment_amount = over_limit_amount
        payment_plan_type = "stop_or_prepayment"
    elif over_limit_amount <= 0:
        first_payment_amount = Decimal("0.00")
        payment_plan_type = "inside_limit"
    elif monthly_payment_cap <= 0:
        first_payment_amount = over_limit_amount
        payment_plan_type = "no_sales_history"
    else:
        first_payment_amount = _money(min(over_limit_amount, monthly_payment_cap))
        payment_plan_type = "monthly_sales_cap"
    first_payment_pct = _percent(first_payment_amount, _money(current_balance)) or Decimal("0.00")
    return CreditPolicyMetrics(
        credit_discipline_grade=grade,
        credit_discipline_label=CREDIT_DISCIPLINE_LABELS_RU[grade],
        credit_discipline_coefficient=coefficient,
        avg_monthly_sales_90=avg_monthly_sales_90,
        base_credit_limit_rate=BASE_CREDIT_LIMIT_RATE,
        recommended_credit_limit=recommended_credit_limit,
        over_limit_amount=over_limit_amount,
        recommended_first_payment_amount=first_payment_amount,
        recommended_first_payment_pct=first_payment_pct,
        payment_plan_type=payment_plan_type,
        policy_note=_credit_policy_note(
            grade=grade,
            over_limit_amount=over_limit_amount,
            monthly_payment_cap=monthly_payment_cap,
            payment_form=payment_form,
        ),
    )


def _credit_discipline_grade(
    *,
    behavior_group: str,
    current_balance: Decimal,
    overdue_days: int | None,
    sales: SalesWindowMetrics,
    payments: PaymentWindowMetrics,
    payment_form: PaymentFormMetrics,
) -> str:
    if _money(current_balance) <= 0:
        return "B" if sales.sales_90 > 0 else "C"
    overdue_days = overdue_days or 0
    debt_ratio = payments.debt_to_sales_90_ratio
    if behavior_group == "chronic_non_payer":
        return "E"
    if overdue_days >= 60 and (debt_ratio is None or debt_ratio >= Decimal("1.00")):
        return "E"
    if behavior_group in {"promise_breaker", "silent_no_contact", "growing_debt_second_week"}:
        return "D"
    if overdue_days >= 30 or sales.trend_coefficient < Decimal("0.85"):
        return "D"
    if behavior_group == "no_current_debt":
        return "B" if sales.sales_90 > 0 else "C"
    if behavior_group in {"partial_but_alive", "new_no_history", "dispute_quality"}:
        return "C"
    if (
        behavior_group in {"weekly_batch_payer", "regular_term_payer"}
        and overdue_days <= 7
        and (payments.payment_to_sales_90_pct or Decimal("0")) >= Decimal("80.00")
        and sales.trend_coefficient >= Decimal("0.85")
    ):
        return _adjust_grade_for_payment_form("A", payment_form=payment_form)
    if behavior_group in {"weekly_batch_payer", "regular_term_payer"}:
        return _adjust_grade_for_payment_form("B", payment_form=payment_form)
    return "C"


def _adjust_grade_for_payment_form(grade: str, *, payment_form: PaymentFormMetrics) -> str:
    if payment_form.payment_form_primary == "cash" and grade == "A":
        return "B"
    return grade


def _credit_policy_note(
    *,
    grade: str,
    over_limit_amount: Decimal,
    monthly_payment_cap: Decimal,
    payment_form: PaymentFormMetrics,
) -> str:
    if grade == "E":
        return "Кредитный лимит 0: стоп отгрузка или предоплата до отдельного решения."
    if over_limit_amount <= 0:
        return "Текущий долг внутри расчетного кредитного лимита."
    note = (
        "Первый платеж рассчитан как сумма возврата долга внутрь лимита, "
        f"но не выше 30% средней месячной продажи ({monthly_payment_cap} руб.)."
    )
    if payment_form.source_status != "ready":
        note += f" Форма оплаты пока не учтена: {payment_form.source_note}"
    return note


def build_advisor_recommendation(
    *,
    behavior_group: str,
    current_balance: Decimal,
    overdue_days: int | None,
    sales: SalesWindowMetrics,
    payments: PaymentWindowMetrics,
    credit_policy: CreditPolicyMetrics,
) -> AdvisorRecommendation:
    if _money(current_balance) <= 0:
        overdue_days = None
    payment_window_days = _recommended_payment_window_days(
        behavior_group=behavior_group,
        overdue_days=overdue_days,
        trend_coefficient=sales.trend_coefficient,
    )
    recommended_decision = _recommended_decision(
        behavior_group=behavior_group,
        current_balance=current_balance,
        overdue_days=overdue_days,
        debt_to_sales_90_ratio=payments.debt_to_sales_90_ratio,
    )
    tone = _advisor_tone(behavior_group=behavior_group, recommended_decision=recommended_decision)
    return AdvisorRecommendation(
        recommended_decision=recommended_decision,
        recommended_decision_label=RECOMMENDED_DECISION_LABELS_RU[recommended_decision],
        recommended_first_payment_pct=credit_policy.recommended_first_payment_pct,
        recommended_first_payment_amount=credit_policy.recommended_first_payment_amount,
        recommended_payment_window_days=payment_window_days,
        advisor_tone=tone,
        advisor_tone_label=ADVISOR_TONE_LABELS_RU[tone],
        advisor_summary=_advisor_summary(
            behavior_group=behavior_group,
            current_balance=current_balance,
            sales=sales,
            payments=payments,
        ),
        negotiation_goal=(
            f"Вернуть долг в расчетный кредитный лимит {credit_policy.recommended_credit_limit} "
            f"руб.: первый платеж {credit_policy.recommended_first_payment_amount} руб. "
            f"({credit_policy.recommended_first_payment_pct}% долга) и закрепить дату закрытия в течение "
            f"{payment_window_days} дней."
        ),
        talk_track=_talk_track(behavior_group),
        allowed_concession=(
            f"Можно обсуждать рассрочку на {payment_window_days} дней после первого платежа "
            f"{credit_policy.recommended_first_payment_amount} руб.; скидку/списание оформлять "
            "только отдельным запросом."
        ),
        forbidden_promises=(
            "Не обещать снятие долга, изменение суммы в 1С, скидку или новую отгрузку "
            "без согласования старшего и финансового подтверждения."
        ),
        escalation_trigger=_escalation_trigger(behavior_group),
    )


def build_portrait_summary(
    portraits: Sequence[ReceivableDecisionPortrait],
) -> dict[str, object]:
    behavior_counts: dict[str, int] = defaultdict(int)
    decision_counts: dict[str, int] = defaultdict(int)
    total_balance = Decimal("0.00")
    partial_count = 0
    for portrait in portraits:
        behavior_counts[portrait.payment_behavior_group] += 1
        decision_counts[portrait.advisor.recommended_decision] += 1
        total_balance += portrait.current_balance
        if portrait.source_status != "ready":
            partial_count += 1
    return {
        "items": len(portraits),
        "total_balance": str(_money(total_balance)),
        "partial_source_items": partial_count,
        "behavior_counts": dict(sorted(behavior_counts.items())),
        "recommended_decision_counts": dict(sorted(decision_counts.items())),
    }


def _load_snapshots(
    session: Session,
    *,
    snapshot_date: date,
    limit: int | None,
    counterparty_refs: Sequence[str],
) -> list[ReceivableBalanceSnapshot]:
    query = select(ReceivableBalanceSnapshot).where(
        ReceivableBalanceSnapshot.snapshot_date == snapshot_date,
        ReceivableBalanceSnapshot.current_balance > 0,
    )
    if counterparty_refs:
        query = query.where(ReceivableBalanceSnapshot.counterparty_ref.in_(counterparty_refs))
    query = query.order_by(
        ReceivableBalanceSnapshot.current_balance.desc(),
        ReceivableBalanceSnapshot.counterparty_name.asc(),
    )
    if limit is not None:
        query = query.limit(limit)
    return list(session.execute(query).scalars().all())


def _load_events_by_counterparty(
    session: Session,
    *,
    snapshot_date: date,
    counterparty_refs: Sequence[str],
) -> dict[str, list[ReceivableLedgerEvent]]:
    if not counterparty_refs:
        return {}
    start_at = datetime.combine(
        snapshot_date - timedelta(days=WINDOW_90_DAYS - 1),
        time.min,
    )
    end_at = datetime.combine(snapshot_date + timedelta(days=1), time.min)
    query = (
        select(ReceivableLedgerEvent)
        .where(
            ReceivableLedgerEvent.counterparty_ref.in_(counterparty_refs),
            ReceivableLedgerEvent.external_document_date >= start_at,
            ReceivableLedgerEvent.external_document_date < end_at,
        )
        .order_by(
            ReceivableLedgerEvent.counterparty_ref,
            ReceivableLedgerEvent.external_document_date,
            ReceivableLedgerEvent.id,
        )
    )
    result: dict[str, list[ReceivableLedgerEvent]] = defaultdict(list)
    for event in session.execute(query).scalars():
        result[event.counterparty_ref].append(event)
    return result


def _sum_sale_events(
    events: Sequence[ReceivableLedgerEvent],
    *,
    snapshot_date: date,
    days: int,
) -> Decimal:
    total = sum(
        Decimal(event.amount_delta)
        for event in events
        if _is_within_window(event, snapshot_date=snapshot_date, days=days)
        and event.event_type == EVENT_SALE
        and Decimal(event.amount_delta) > 0
    )
    return _money(total)


def _sum_payment_events(
    events: Sequence[ReceivableLedgerEvent],
    *,
    snapshot_date: date,
    days: int,
) -> Decimal:
    total = sum(
        abs(Decimal(event.amount_delta))
        for event in events
        if _is_within_window(event, snapshot_date=snapshot_date, days=days)
        and _is_payment_event(event)
    )
    return _money(total)


def _sum_return_events(
    events: Sequence[ReceivableLedgerEvent],
    *,
    snapshot_date: date,
) -> Decimal:
    total = sum(
        abs(Decimal(event.amount_delta))
        for event in events
        if _is_within_window(event, snapshot_date=snapshot_date, days=WINDOW_90_DAYS)
        and event.event_type == EVENT_RETURN
    )
    return _money(total)


def _is_payment_event(event: ReceivableLedgerEvent) -> bool:
    return event.event_type in {EVENT_PAYMENT, EVENT_SETTLEMENT} and Decimal(event.amount_delta) < 0


def _is_within_window(
    event: ReceivableLedgerEvent,
    *,
    snapshot_date: date,
    days: int,
) -> bool:
    event_date = event.external_document_date.date()
    start_date = snapshot_date - timedelta(days=days - 1)
    return start_date <= event_date <= snapshot_date


def _safe_daily_average(amount: Decimal, days: int) -> Decimal:
    if days <= 0:
        return Decimal("0.00")
    return _money(amount / Decimal(days))


def _percent(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    if denominator <= 0:
        return None
    return _ratio(numerator / denominator * Decimal("100"))


def _covers_sales(payment_total: Decimal, sales_total: Decimal, threshold: Decimal) -> bool:
    if sales_total <= 0:
        return payment_total > 0
    return payment_total >= sales_total * threshold


def _recommended_payment_window_days(
    *,
    behavior_group: str,
    overdue_days: int | None,
    trend_coefficient: Decimal,
) -> int:
    overdue_days = overdue_days or 0
    if behavior_group in {
        "chronic_non_payer",
        "growing_debt_second_week",
        "promise_breaker",
        "silent_no_contact",
    }:
        return 7
    if overdue_days >= 30 or trend_coefficient < Decimal("0.85"):
        return 7
    return 10


def _recommended_decision(
    *,
    behavior_group: str,
    current_balance: Decimal,
    overdue_days: int | None,
    debt_to_sales_90_ratio: Decimal | None,
) -> str:
    if _money(current_balance) <= 0:
        return "soft_work"
    overdue_days = overdue_days or 0
    if behavior_group == "dispute_quality":
        return "verify_amount"
    if behavior_group == "chronic_non_payer":
        return "shipment_stop"
    if overdue_days >= 60 and (debt_to_sales_90_ratio is None or debt_to_sales_90_ratio >= 1):
        return "shipment_stop"
    if behavior_group in {"growing_debt_second_week", "promise_breaker", "silent_no_contact"}:
        return "strict_control"
    if behavior_group == "partial_but_alive":
        return "strict_control"
    if behavior_group in {"weekly_batch_payer", "regular_term_payer"}:
        return "soft_work"
    return "strict_control" if overdue_days >= 14 else "soft_work"


def _advisor_tone(*, behavior_group: str, recommended_decision: str) -> str:
    if recommended_decision == "verify_amount":
        return "fact_check"
    if recommended_decision == "shipment_stop":
        return "final_warning"
    if behavior_group in {"weekly_batch_payer", "regular_term_payer", "no_current_debt"}:
        return "partner_soft"
    return "calm_strict"


def _advisor_summary(
    *,
    behavior_group: str,
    current_balance: Decimal,
    sales: SalesWindowMetrics,
    payments: PaymentWindowMetrics,
) -> str:
    label = PAYMENT_BEHAVIOR_LABELS_RU[behavior_group]
    trend = (
        "рост"
        if sales.trend_coefficient > 1
        else "снижение" if sales.trend_coefficient < 1 else "ровно"
    )
    paid = payments.payment_to_sales_90_pct
    if paid is None:
        paid_text = ""
    elif paid <= Decimal("300"):
        paid_text = f", оплачено {paid}% от продаж 90 дней"
    else:
        paid_text = f", оплаты 90 дней {payments.payment_total_90} руб."
    return (
        f"{label}: долг {current_balance} руб., продажи 90 дней {sales.sales_90} руб., "
        f"тренд {trend} ({sales.trend_coefficient}){paid_text}."
    )


def _talk_track(behavior_group: str) -> str:
    return {
        "weekly_batch_payer": (
            "Начать с привычного ритма оплат: подтвердить ближайший день недельной оплаты "
            "и зафиксировать сумму первого платежа."
        ),
        "regular_term_payer": (
            "Говорить партнерски: показать сумму, срок и попросить подтвердить дату оплаты "
            "без давления на конфликт."
        ),
        "partial_but_alive": (
            "Признать, что платежи идут, но зафиксировать конкретный первый платеж и короткий "
            "график закрытия остатка."
        ),
        "growing_debt_second_week": (
            "Сразу обозначить рост долга: новые заказы возможны только при понятном первом "
            "платеже и графике."
        ),
        "chronic_non_payer": (
            "Держать факты и границы: сумма долга больше покупок за период, нужен платеж до "
            "дальнейших отгрузок."
        ),
        "promise_breaker": (
            "Напомнить сорванную договоренность и перевести разговор в подтвержденный платеж "
            "с коротким сроком."
        ),
        "silent_no_contact": (
            "Коротко фиксировать попытку связи, запросить подтверждение долга и альтернативный "
            "контакт для оплаты."
        ),
        "dispute_quality": (
            "Не спорить по телефону: собрать номер документа, причину расхождения и поставить "
            "проверку суммы."
        ),
        "new_no_history": (
            "Собрать базовую картину клиента, подтвердить долг, контакт и первый понятный срок "
            "оплаты."
        ),
        "no_current_debt": (
            "Долга сейчас нет: подтвердить комфортный лимит и условия оплаты до следующей отгрузки."
        ),
    }[behavior_group]


def _escalation_trigger(behavior_group: str) -> str:
    if behavior_group == "dispute_quality":
        return "Эскалировать, если клиент называет брак/возврат, а документа или решения нет."
    if behavior_group == "chronic_non_payer":
        return "Эскалировать при отказе от первого платежа или просьбе новой отгрузки без оплаты."
    if behavior_group == "growing_debt_second_week":
        return "Эскалировать, если клиент не подтверждает платеж в течение 7 дней."
    return "Эскалировать, если нет контакта или подтвержденной даты оплаты после двух попыток."


def _source_notes(
    *,
    profitability: ProfitabilityWindowMetrics,
    sales: SalesWindowMetrics,
) -> list[str]:
    notes: list[str] = []
    if profitability.source_status != "ready":
        notes.append(profitability.source_note)
    if sales.return_filter_status != "ready":
        notes.append(
            "Возвраты по причине брака пока не отделены: в текущем журнале нет причины возврата."
        )
    return notes


def _to_date(value: datetime | date | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    return value


def _folder_key(value: object) -> str:
    return str(value or "").strip().casefold().replace("ё", "е")


def _money(value: Decimal | int | float | str) -> Decimal:
    return Decimal(value).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def _ratio(value: Decimal | int | float | str) -> Decimal:
    return Decimal(value).quantize(RATIO_QUANT, rounding=ROUND_HALF_UP)


def _json_ready(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_json_ready(item) for item in value]
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    return value
