from __future__ import annotations

import csv
import json
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, ReceivableBalanceSnapshot, ReceivableLedgerEvent
from app.services import bi as bi_service
from tasks import compare_receivable_current_report as compare_receivable_current_report_task
from tasks.compare_receivable_current_report import compare_receivable_current_report


def _write_current_report_csv(
    path: Path,
    *,
    snapshot_date: date,
    balances: list[tuple[str, str]],
) -> Path:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["snapshot_date", "counterparty_name", "current_balance_rub", "source_row"])
        for index, (counterparty_name, current_balance_rub) in enumerate(balances, start=1):
            writer.writerow(
                [snapshot_date.isoformat(), counterparty_name, current_balance_rub, index]
            )
    return path


def _snapshot(
    *,
    snapshot_date: date,
    counterparty_ref: str,
    counterparty_name: str,
    current_balance: str,
) -> ReceivableBalanceSnapshot:
    return ReceivableBalanceSnapshot(
        snapshot_date=snapshot_date,
        counterparty_ref=counterparty_ref,
        counterparty_name=counterparty_name,
        current_balance=Decimal(current_balance),
        activity_segment="active",
        aged_bucket="0-30",
        is_overdue=False,
    )


def _event(
    *,
    business_key: str,
    event_type: str,
    external_document_ref: str,
    external_document_number: str,
    external_document_date: datetime,
    counterparty_ref: str,
    counterparty_name: str,
    contract_ref: str,
    contract_name: str,
    contract_kind_name: str,
    amount_delta: str,
) -> ReceivableLedgerEvent:
    return ReceivableLedgerEvent(
        source="onec",
        business_key=business_key,
        event_type=event_type,
        external_document_ref=external_document_ref,
        external_document_number=external_document_number,
        external_document_date=external_document_date,
        counterparty_ref=counterparty_ref,
        counterparty_name=counterparty_name,
        contract_ref=contract_ref,
        contract_name=contract_name,
        contract_kind_ref=f"kind:{contract_kind_name}",
        contract_kind_name=contract_kind_name,
        manager_ref="mgr-1",
        manager_name="Менеджер 1",
        store_ref="store-1",
        store_name="Магазин 1",
        source_layer="regular_receivables",
        amount_delta=Decimal(amount_delta),
    )


def test_compare_receivable_current_report_cli_uses_role_specific_read_only_db_access(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    report_path = tmp_path / "current.csv"
    report_path.write_text("placeholder", encoding="utf-8")
    session = object()
    result = {"status": "compared", "snapshot_date": "2026-08-28"}
    scope_calls: list[bool] = []
    engine_calls: list[tuple[str, int, int]] = []
    compare_calls: list[tuple[object, Path, str, object, int]] = []

    class FakeOnecEngine:
        disposed = False

        def dispose(self) -> None:
            self.disposed = True

    onec_engine = FakeOnecEngine()

    @contextmanager
    def fake_session_scope(*, read_only: bool = False):
        scope_calls.append(read_only)
        yield session

    def fake_build_onec_engine(
        database_url: str,
        *,
        query_timeout_seconds: int,
        login_timeout_seconds: int,
    ) -> FakeOnecEngine:
        engine_calls.append((database_url, query_timeout_seconds, login_timeout_seconds))
        return onec_engine

    def fake_compare(
        current_session: object,
        current_report_path: Path,
        *,
        counterparty_filter_mode: str,
        onec_engine: object,
        top: int,
    ) -> dict[str, str]:
        compare_calls.append(
            (
                current_session,
                current_report_path,
                counterparty_filter_mode,
                onec_engine,
                top,
            )
        )
        return result

    monkeypatch.setattr(
        compare_receivable_current_report_task,
        "get_settings",
        lambda: SimpleNamespace(
            onec_database_url="mssql+pyodbc://onec",
            onec_query_timeout_seconds=45,
            onec_login_timeout_seconds=7,
        ),
    )
    monkeypatch.setattr(
        compare_receivable_current_report_task,
        "session_scope",
        fake_session_scope,
    )
    monkeypatch.setattr(
        compare_receivable_current_report_task,
        "build_onec_engine",
        fake_build_onec_engine,
    )
    monkeypatch.setattr(
        compare_receivable_current_report_task,
        "compare_receivable_current_report",
        fake_compare,
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "compare_receivable_current_report",
            str(report_path),
            "--top",
            "11",
            "--compare-onec-canonical",
            "--counterparty-filter-mode",
            "all",
        ],
    )

    compare_receivable_current_report_task.main()

    assert scope_calls == [True]
    assert engine_calls == [("mssql+pyodbc://onec", 45, 7)]
    assert compare_calls == [(session, report_path, "all", onec_engine, 11)]
    assert onec_engine.disposed is True
    assert json.loads(capsys.readouterr().out) == result


def test_compare_receivable_current_report_matches_direct_month_end_snapshot(
    tmp_path: Path,
    monkeypatch,
) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    report_path = _write_current_report_csv(
        tmp_path / "current-2026-03-31.csv",
        snapshot_date=date(2026, 3, 31),
        balances=[
            ("Контрагент A", "100.00"),
            ("Контрагент B", "50.00"),
        ],
    )

    with Session(engine) as session:
        session.add_all(
            [
                _snapshot(
                    snapshot_date=date(2026, 3, 31),
                    counterparty_ref="cp-a",
                    counterparty_name="Контрагент A",
                    current_balance="100.00",
                ),
                _snapshot(
                    snapshot_date=date(2026, 3, 31),
                    counterparty_ref="cp-b",
                    counterparty_name="Контрагент B",
                    current_balance="50.00",
                ),
            ]
        )
        session.commit()

        monkeypatch.setattr(
            bi_service,
            "_buyers_counterparty_refs_from_onec",
            lambda: ("cp-a", "cp-b"),
        )
        result = compare_receivable_current_report(session, report_path, top=10)

    assert result["snapshot_date"] == date(2026, 3, 31)
    assert result["file"]["total_balance"] == Decimal("150.00")
    assert result["balance_snapshot"]["exact_match"] is True
    assert result["balance_snapshot"]["candidate_minus_file_total"] == Decimal("0.00")
    assert result["buyers_rub_only"]["mode"] == "direct_snapshot"
    assert result["buyers_rub_only"]["base_snapshot_date"] is None
    assert result["buyers_rub_only"]["exact_match"] is True
    assert result["buyers_rub_only"]["candidate_minus_file_total"] == Decimal("0.00")


def test_compare_receivable_current_report_buyers_rub_only_uses_exact_snapshot_only(
    tmp_path: Path,
    monkeypatch,
) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    report_path = _write_current_report_csv(
        tmp_path / "current-2026-04-04.csv",
        snapshot_date=date(2026, 4, 4),
        balances=[
            ("Контрагент A", "90.00"),
            ("Контрагент B", "50.00"),
            ("Контрагент C", "30.00"),
        ],
    )

    with Session(engine) as session:
        session.add_all(
            [
                _snapshot(
                    snapshot_date=date(2026, 3, 31),
                    counterparty_ref="cp-a",
                    counterparty_name="Контрагент A",
                    current_balance="100.00",
                ),
                _snapshot(
                    snapshot_date=date(2026, 3, 31),
                    counterparty_ref="cp-b",
                    counterparty_name="Контрагент B",
                    current_balance="50.00",
                ),
                _snapshot(
                    snapshot_date=date(2026, 4, 4),
                    counterparty_ref="wrong-cp",
                    counterparty_name="Неправильный общий контур",
                    current_balance="-200000000.00",
                ),
            ]
        )
        session.add_all(
            [
                _event(
                    business_key="cp-a-pay-0401",
                    event_type="payment",
                    external_document_ref="pay-a-1",
                    external_document_number="P-A-1",
                    external_document_date=datetime(2026, 4, 1, 11, 0, 0),
                    counterparty_ref="cp-a",
                    counterparty_name="Контрагент A",
                    contract_ref="contract-a",
                    contract_name="Договор A",
                    contract_kind_name="С покупателем",
                    amount_delta="-20.00",
                ),
                _event(
                    business_key="cp-a-sale-0403",
                    event_type="sale",
                    external_document_ref="sale-a-2",
                    external_document_number="S-A-2",
                    external_document_date=datetime(2026, 4, 3, 12, 0, 0),
                    counterparty_ref="cp-a",
                    counterparty_name="Контрагент A",
                    contract_ref="contract-a",
                    contract_name="Договор A",
                    contract_kind_name="С покупателем",
                    amount_delta="10.00",
                ),
                _event(
                    business_key="cp-c-sale-0404",
                    event_type="sale",
                    external_document_ref="sale-c-1",
                    external_document_number="S-C-1",
                    external_document_date=datetime(2026, 4, 4, 13, 0, 0),
                    counterparty_ref="cp-c",
                    counterparty_name="Контрагент C",
                    contract_ref="contract-c",
                    contract_name="Договор C",
                    contract_kind_name="С покупателем",
                    amount_delta="30.00",
                ),
                _event(
                    business_key="cp-x-sale-0402",
                    event_type="sale",
                    external_document_ref="sale-x-1",
                    external_document_number="S-X-1",
                    external_document_date=datetime(2026, 4, 2, 14, 0, 0),
                    counterparty_ref="cp-x",
                    counterparty_name="Контрагент X",
                    contract_ref="contract-x",
                    contract_name="Договор X",
                    contract_kind_name="С поставщиком",
                    amount_delta="999.00",
                ),
            ]
        )
        session.commit()

        monkeypatch.setattr(
            bi_service,
            "_buyers_counterparty_refs_from_onec",
            lambda: ("cp-a", "cp-b", "cp-c"),
        )
        result = compare_receivable_current_report(session, report_path, top=10)

    assert result["snapshot_date"] == date(2026, 4, 4)
    assert result["file"]["total_balance"] == Decimal("170.00")

    assert result["balance_snapshot"]["exact_match"] is False
    assert result["balance_snapshot"]["candidate_minus_file_total"] == Decimal("-200000170.00")
    assert result["balance_snapshot"]["mismatch_count"] == 4

    assert result["buyers_rub_only"]["mode"] == "direct_snapshot"
    assert result["buyers_rub_only"]["base_snapshot_date"] is None
    assert result["buyers_rub_only"]["total_balance"] == Decimal("0.00")
    assert result["buyers_rub_only"]["exact_match"] is False
    assert result["buyers_rub_only"]["candidate_minus_file_total"] == Decimal("-170.00")
