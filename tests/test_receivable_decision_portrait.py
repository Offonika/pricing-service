from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import tasks.build_receivable_decision_portraits as portrait_task
from app.models import Base
from app.models.counterparty_folder_snapshot import CounterpartyFolderSnapshot
from app.models.receivable_balance_snapshot import ReceivableBalanceSnapshot
from app.models.receivable_ledger_event import ReceivableLedgerEvent
from app.services.receivable_decision_portrait import (
    ProfitabilityWindowMetrics,
    build_portrait_summary,
    build_receivable_decision_portrait,
    build_receivable_decision_portraits,
    compute_trend_coefficient,
)

SNAPSHOT_DATE = date(2026, 7, 4)


def test_compute_trend_coefficient_caps_growth_and_decline() -> None:
    assert compute_trend_coefficient(
        sales_30=Decimal("900"),
        sales_90=Decimal("900"),
    ) == Decimal("1.20")
    assert compute_trend_coefficient(
        sales_30=Decimal("90"),
        sales_90=Decimal("900"),
    ) == Decimal("0.50")
    assert compute_trend_coefficient(
        sales_30=Decimal("0"),
        sales_90=Decimal("0"),
    ) == Decimal("1.00")


def test_build_receivable_decision_portraits_groups_payment_behavior(db_session: Session) -> None:
    _add_snapshot(
        db_session,
        counterparty_ref="cp-weekly",
        counterparty_name="Ежедневный клиент",
        current_balance=Decimal("1000"),
        overdue_days=5,
    )
    _add_snapshot(
        db_session,
        counterparty_ref="cp-chronic",
        counterparty_name="Злостный клиент",
        current_balance=Decimal("20000"),
        overdue_days=45,
    )
    for index, days_ago in enumerate([2, 5, 8, 12, 16, 20, 24, 28], start=1):
        _add_event(
            db_session,
            counterparty_ref="cp-weekly",
            business_key=f"weekly-sale-{index}",
            event_type="sale",
            days_ago=days_ago,
            amount=Decimal("1000"),
        )
    for index, days_ago in enumerate([7, 14, 21, 28], start=1):
        _add_event(
            db_session,
            counterparty_ref="cp-weekly",
            business_key=f"weekly-payment-{index}",
            event_type="payment",
            days_ago=days_ago,
            amount=Decimal("-2000"),
        )
    for index, days_ago in enumerate([10, 20, 30, 40, 50], start=1):
        _add_event(
            db_session,
            counterparty_ref="cp-chronic",
            business_key=f"chronic-sale-{index}",
            event_type="sale",
            days_ago=days_ago,
            amount=Decimal("1000"),
        )
    _add_event(
        db_session,
        counterparty_ref="cp-chronic",
        business_key="chronic-payment-1",
        event_type="payment",
        days_ago=25,
        amount=Decimal("-1000"),
    )
    db_session.commit()

    portraits = build_receivable_decision_portraits(
        db_session,
        snapshot_date=SNAPSHOT_DATE,
    )
    by_ref = {portrait.counterparty_ref: portrait for portrait in portraits}

    weekly = by_ref["cp-weekly"]
    assert weekly.payment_behavior_group == "weekly_batch_payer"
    assert weekly.sales.sales_90 == Decimal("8000.00")
    assert weekly.payments.payment_total_90 == Decimal("8000.00")
    assert weekly.credit_policy.credit_discipline_grade == "A"
    assert weekly.credit_policy.recommended_credit_limit == Decimal("960.00")
    assert weekly.credit_policy.over_limit_amount == Decimal("40.00")
    assert weekly.credit_policy.recommended_first_payment_amount == Decimal("40.00")
    assert weekly.advisor.recommended_decision == "soft_work"
    assert weekly.advisor.recommended_first_payment_pct == Decimal("4.00")

    chronic = by_ref["cp-chronic"]
    assert chronic.payment_behavior_group == "chronic_non_payer"
    assert chronic.credit_policy.credit_discipline_grade == "E"
    assert chronic.credit_policy.credit_discipline_coefficient == Decimal("0.00")
    assert chronic.credit_policy.recommended_credit_limit == Decimal("0.00")
    assert chronic.credit_policy.recommended_first_payment_amount == Decimal("20000.00")
    assert chronic.advisor.recommended_decision == "shipment_stop"
    assert chronic.advisor.recommended_first_payment_pct == Decimal("100.00")
    assert chronic.advisor.recommended_payment_window_days == 7
    assert chronic.profitability.source_status == "missing_counterparty_cost"
    assert chronic.source_status == "partial"

    summary = build_portrait_summary(portraits)
    assert summary["items"] == 2
    assert summary["total_balance"] == "21000.00"
    assert summary["behavior_counts"] == {
        "chronic_non_payer": 1,
        "weekly_batch_payer": 1,
    }


def test_non_positive_balance_cannot_be_credit_risk_from_stale_overdue_date(
    db_session: Session,
) -> None:
    _add_snapshot(
        db_session,
        counterparty_ref="cp-change",
        counterparty_name="Клиент со сдачей",
        current_balance=Decimal("-50.00"),
        overdue_days=18444,
    )
    db_session.commit()
    snapshot = (
        db_session.query(ReceivableBalanceSnapshot).filter_by(counterparty_ref="cp-change").one()
    )

    portrait = build_receivable_decision_portrait(snapshot, events=[])

    assert portrait.current_balance == Decimal("-50.00")
    assert portrait.overdue_days is None
    assert portrait.due_date is None
    assert portrait.payment_behavior_group == "no_current_debt"
    assert portrait.credit_policy.credit_discipline_grade == "C"
    assert portrait.credit_policy.over_limit_amount == Decimal("0.00")
    assert portrait.credit_policy.recommended_first_payment_amount == Decimal("0.00")
    assert portrait.advisor.recommended_decision == "soft_work"


def test_task_exports_local_json_and_csv(tmp_path) -> None:
    db_path = tmp_path / "receivables.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        _add_snapshot(
            session,
            counterparty_ref="cp-export",
            counterparty_name="Клиент для выгрузки",
            current_balance=Decimal("5000"),
            overdue_days=20,
        )
        _add_event(
            session,
            counterparty_ref="cp-export",
            business_key="export-sale-1",
            event_type="sale",
            days_ago=5,
            amount=Decimal("3000"),
        )
        _add_folder_snapshot(session, counterparty_ref="cp-export", folder_name="Покупатели")
        session.commit()

    output_dir = tmp_path / "out"
    exit_code = portrait_task.main(
        [
            "--database-url",
            f"sqlite:///{db_path}",
            "--snapshot-date",
            SNAPSHOT_DATE.isoformat(),
            "--output-dir",
            str(output_dir),
            "--json",
        ]
    )

    assert exit_code == 0
    json_path = output_dir / SNAPSHOT_DATE.isoformat() / "receivable-decision-portraits.json"
    csv_path = output_dir / SNAPSHOT_DATE.isoformat() / "receivable-decision-portraits.csv"
    assert json_path.exists()
    assert csv_path.exists()
    payload = json_path.read_text(encoding="utf-8")
    assert "Дебиторка Решение" in payload
    assert "Клиент для выгрузки" in payload
    assert '"bitrix_writes": false' in payload
    assert '"folder_name": "Покупатели"' in payload
    csv_text = csv_path.read_text(encoding="utf-8-sig")
    assert "credit_discipline_grade" in csv_text
    assert "recommended_credit_limit" in csv_text
    assert "payment_form_source_status" in csv_text


def test_task_requires_buyer_folder_snapshot_by_default(tmp_path) -> None:
    db_path = tmp_path / "receivables.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        _add_snapshot(
            session,
            counterparty_ref="cp-without-folder-snapshot",
            counterparty_name="Клиент без снимка папки",
            current_balance=Decimal("5000"),
            overdue_days=20,
        )
        session.commit()

    try:
        portrait_task.main(
            [
                "--database-url",
                f"sqlite:///{db_path}",
                "--snapshot-date",
                SNAPSHOT_DATE.isoformat(),
                "--output-dir",
                str(tmp_path / "out"),
                "--json",
            ]
        )
    except SystemExit as exc:
        assert "нельзя безопасно ограничить расчет папкой" in str(exc)
    else:
        raise AssertionError("task must stop when buyer folder snapshot is missing")


def test_profitability_metrics_mark_portrait_ready(db_session: Session) -> None:
    _add_snapshot(
        db_session,
        counterparty_ref="cp-profit",
        counterparty_name="Клиент с прибылью",
        current_balance=Decimal("10000"),
        overdue_days=8,
    )
    _add_event(
        db_session,
        counterparty_ref="cp-profit",
        business_key="profit-sale-1",
        event_type="sale",
        days_ago=5,
        amount=Decimal("20000"),
    )
    db_session.commit()

    portraits = build_receivable_decision_portraits(
        db_session,
        snapshot_date=SNAPSHOT_DATE,
        profitability_by_ref={
            "CP-PROFIT": ProfitabilityWindowMetrics(
                revenue_90=Decimal("20000.00"),
                cost_of_sales_90=Decimal("13000.00"),
                gross_profit_90=Decimal("7000.00"),
                gross_margin_pct_90=Decimal("35.00"),
                profitability_pct_90=Decimal("53.85"),
                defect_return_amount_90=Decimal("1000.00"),
                source_status="ready",
                source_note="1С read-only",
            )
        },
    )

    portrait = portraits[0]
    assert portrait.source_status == "ready"
    assert portrait.profitability.gross_profit_90 == Decimal("7000.00")
    assert portrait.sales.return_filter_status == "ready"
    assert portrait.sales.defect_return_amount_90 == Decimal("1000.00")


def _add_snapshot(
    session: Session,
    *,
    counterparty_ref: str,
    counterparty_name: str,
    current_balance: Decimal,
    overdue_days: int,
) -> None:
    session.add(
        ReceivableBalanceSnapshot(
            snapshot_date=SNAPSHOT_DATE,
            counterparty_ref=counterparty_ref,
            counterparty_code=counterparty_ref.upper(),
            counterparty_name=counterparty_name,
            current_balance=current_balance,
            current_manager_ref="manager-1",
            current_manager_name="Менеджер",
            department_ref="dept-1",
            department_name="Покупатели",
            due_date=datetime.combine(SNAPSHOT_DATE - timedelta(days=overdue_days), time.min),
            overdue_days=overdue_days,
            is_overdue=overdue_days > 0,
            aged_bucket="30",
            activity_segment="active",
        )
    )


def _add_event(
    session: Session,
    *,
    counterparty_ref: str,
    business_key: str,
    event_type: str,
    days_ago: int,
    amount: Decimal,
) -> None:
    session.add(
        ReceivableLedgerEvent(
            source="onec",
            business_key=business_key,
            event_type=event_type,
            external_document_ref=business_key,
            external_document_number=business_key,
            external_document_date=datetime.combine(
                SNAPSHOT_DATE - timedelta(days=days_ago),
                time(hour=12),
            ),
            counterparty_ref=counterparty_ref,
            counterparty_name=counterparty_ref,
            contract_ref="contract-1",
            contract_name="Основной договор",
            contract_kind_ref="buyer-kind",
            contract_kind_name="С покупателем",
            source_layer="regular_receivables",
            amount_delta=amount,
        )
    )


def _add_folder_snapshot(
    session: Session,
    *,
    counterparty_ref: str,
    folder_name: str,
) -> None:
    session.add(
        CounterpartyFolderSnapshot(
            snapshot_date=SNAPSHOT_DATE,
            counterparty_ref=counterparty_ref,
            counterparty_name=counterparty_ref,
            current_folder_ref=f"folder-{folder_name}",
            current_folder_name=folder_name,
        )
    )
