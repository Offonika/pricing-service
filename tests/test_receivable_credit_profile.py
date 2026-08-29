from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import tasks.build_receivable_credit_profiles as credit_profile_task
from app.models import Base
from app.models.counterparty_folder_snapshot import CounterpartyFolderSnapshot
from app.models.receivable_balance_snapshot import ReceivableBalanceSnapshot
from app.models.receivable_ledger_event import ReceivableLedgerEvent
from app.services.receivable_credit_profile import build_receivable_credit_profiles

SNAPSHOT_DATE = date(2026, 7, 4)


def test_credit_profile_task_uses_role_specific_read_only_db_lifecycle(
    monkeypatch,
    tmp_path,
) -> None:
    class FakeOnecEngine:
        def __init__(self) -> None:
            self.disposed = False

        def dispose(self) -> None:
            self.disposed = True

    app_session = object()
    session_scope_calls: list[dict[str, object]] = []
    onec_factory_calls: list[dict[str, object]] = []
    onec_engines: list[FakeOnecEngine] = []

    @contextmanager
    def fake_session_scope(*, read_only: bool, database_url: str | None):
        session_scope_calls.append({"read_only": read_only, "database_url": database_url})
        yield app_session

    def fake_build_onec_engine(
        database_url: str,
        *,
        query_timeout_seconds: int | float,
        login_timeout_seconds: int | float,
    ) -> FakeOnecEngine:
        onec_factory_calls.append(
            {
                "database_url": database_url,
                "query_timeout_seconds": query_timeout_seconds,
                "login_timeout_seconds": login_timeout_seconds,
            }
        )
        engine = FakeOnecEngine()
        onec_engines.append(engine)
        return engine

    args = SimpleNamespace(
        snapshot_date=SNAPSHOT_DATE,
        limit=None,
        output_dir=tmp_path,
        database_url="sqlite:///override.db",
        onec_folder="Покупатели",
        folder_filter_source="onec",
        onec_database_url="mssql://override",
        active_window_days=365,
        with_onec_metrics=True,
        allow_missing_folder_filter=False,
    )
    settings = SimpleNamespace(
        database_url="postgresql://settings",
        onec_database_url="mssql://settings",
        onec_query_timeout_seconds=37,
        onec_login_timeout_seconds=11,
    )
    profiles = [SimpleNamespace(counterparty_ref="cp-lifecycle")]
    monkeypatch.setattr(credit_profile_task, "_parse_args", lambda _argv: args)
    monkeypatch.setattr(credit_profile_task, "get_settings", lambda: settings)
    monkeypatch.setattr(credit_profile_task, "session_scope", fake_session_scope)
    monkeypatch.setattr(credit_profile_task, "build_onec_engine", fake_build_onec_engine)
    monkeypatch.setattr(
        credit_profile_task,
        "fetch_counterparty_refs_from_onec_group",
        lambda _engine, **_kwargs: {"cp-lifecycle"},
    )
    monkeypatch.setattr(
        credit_profile_task,
        "build_receivable_credit_profiles",
        lambda *_args, **_kwargs: profiles,
    )
    monkeypatch.setattr(
        credit_profile_task,
        "fetch_counterparty_profitability_metrics_from_onec",
        lambda _engine, **_kwargs: {},
    )
    monkeypatch.setattr(
        credit_profile_task,
        "fetch_counterparty_payment_form_metrics_from_onec",
        lambda _engine, **_kwargs: {},
    )
    monkeypatch.setattr(
        credit_profile_task,
        "build_payload",
        lambda **_kwargs: {"summary": {}, "folder_filter": {}},
    )
    monkeypatch.setattr(credit_profile_task, "write_json", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(credit_profile_task, "write_csv", lambda *_args, **_kwargs: None)

    assert credit_profile_task.main() == 0
    assert session_scope_calls == [{"read_only": True, "database_url": "sqlite:///override.db"}]
    assert onec_factory_calls == [
        {
            "database_url": "mssql://override",
            "query_timeout_seconds": 37,
            "login_timeout_seconds": 11,
        },
        {
            "database_url": "mssql://override",
            "query_timeout_seconds": 37,
            "login_timeout_seconds": 11,
        },
    ]
    assert len(onec_engines) == 2
    assert all(engine.disposed for engine in onec_engines)


def test_build_credit_profiles_includes_active_buyer_without_debt_and_debtor(
    db_session: Session,
) -> None:
    _add_snapshot(
        db_session,
        counterparty_ref="cp-debt",
        counterparty_name="Клиент с долгом",
        current_balance=Decimal("5000"),
        overdue_days=12,
    )
    _add_snapshot(
        db_session,
        counterparty_ref="cp-zero",
        counterparty_name="Активный без долга",
        current_balance=Decimal("0"),
        overdue_days=999,
        credit_depth_days=999,
    )
    _add_snapshot(
        db_session,
        counterparty_ref="cp-inactive",
        counterparty_name="Неактивный покупатель",
        current_balance=Decimal("0"),
        overdue_days=0,
    )
    for index, days_ago in enumerate([5, 15, 25], start=1):
        _add_event(
            db_session,
            counterparty_ref="cp-zero",
            business_key=f"zero-sale-{index}",
            event_type="sale",
            days_ago=days_ago,
            amount=Decimal("10000"),
        )
    for index, days_ago in enumerate([4, 14, 24], start=1):
        _add_event(
            db_session,
            counterparty_ref="cp-zero",
            business_key=f"zero-payment-{index}",
            event_type="payment",
            days_ago=days_ago,
            amount=Decimal("-10000"),
        )
    _add_event(
        db_session,
        counterparty_ref="cp-debt",
        business_key="debt-sale-1",
        event_type="sale",
        days_ago=10,
        amount=Decimal("9000"),
    )
    db_session.commit()

    profiles = build_receivable_credit_profiles(
        db_session,
        snapshot_date=SNAPSHOT_DATE,
        counterparty_refs=["cp-debt", "cp-zero", "cp-inactive"],
    )

    by_ref = {profile.counterparty_ref: profile for profile in profiles}
    assert set(by_ref) == {"cp-debt", "cp-zero"}
    assert by_ref["cp-zero"].current_balance == Decimal("0.00")
    assert by_ref["cp-zero"].credit_depth_days is None
    assert by_ref["cp-zero"].payment_behavior_group == "no_current_debt"
    assert by_ref["cp-zero"].credit_discipline_grade not in {"D", "E"}
    assert by_ref["cp-zero"].recommended_decision != "stop_shipment"
    assert by_ref["cp-zero"].recommended_credit_limit > Decimal("0.00")
    assert by_ref["cp-zero"].recommended_first_payment_amount == Decimal("0.00")
    assert by_ref["cp-zero"].activity_reason == "active_90"
    assert by_ref["cp-debt"].current_balance == Decimal("5000.00")


def test_credit_profile_task_exports_local_json_and_csv(tmp_path) -> None:
    db_path = tmp_path / "receivables.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        _add_snapshot(
            session,
            counterparty_ref="cp-export",
            counterparty_name="Клиент для кредитного профиля",
            current_balance=Decimal("0"),
            overdue_days=0,
        )
        _add_event(
            session,
            counterparty_ref="cp-export",
            business_key="export-sale-1",
            event_type="sale",
            days_ago=5,
            amount=Decimal("30000"),
        )
        _add_event(
            session,
            counterparty_ref="cp-export",
            business_key="export-payment-1",
            event_type="payment",
            days_ago=3,
            amount=Decimal("-30000"),
        )
        _add_folder_snapshot(session, counterparty_ref="cp-export", folder_name="Покупатели")
        session.commit()

    output_dir = tmp_path / "out"
    exit_code = credit_profile_task.main(
        [
            "--database-url",
            f"sqlite:///{db_path}",
            "--snapshot-date",
            SNAPSHOT_DATE.isoformat(),
            "--output-dir",
            str(output_dir),
        ]
    )

    assert exit_code == 0
    json_path = output_dir / SNAPSHOT_DATE.isoformat() / "receivable-credit-profiles.json"
    csv_path = output_dir / SNAPSHOT_DATE.isoformat() / "receivable-credit-profiles.csv"
    assert json_path.exists()
    assert csv_path.exists()
    payload = json_path.read_text(encoding="utf-8")
    assert "Кредитные профили покупателей" in payload
    assert "Клиент для кредитного профиля" in payload
    assert '"bitrix_writes": false' in payload
    assert '"folder_name": "Покупатели"' in payload
    csv_text = csv_path.read_text(encoding="utf-8-sig")
    assert "credit_discipline_grade" in csv_text
    assert "recommended_credit_limit" in csv_text
    assert "payment_form_primary" in csv_text


def _add_snapshot(
    session: Session,
    *,
    counterparty_ref: str,
    counterparty_name: str,
    current_balance: Decimal,
    overdue_days: int,
    credit_depth_days: int | None = None,
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
            credit_depth_days=credit_depth_days,
            due_date=datetime.combine(SNAPSHOT_DATE - timedelta(days=overdue_days), time.min),
            overdue_days=overdue_days,
            is_overdue=overdue_days > 0,
            aged_bucket="0",
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
