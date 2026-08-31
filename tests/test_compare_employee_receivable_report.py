from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.receivables import ReceivableLedgerRow
from tasks import compare_employee_receivable_report as report_task
from tasks.compare_employee_receivable_report import (
    filter_ledger_events,
    parse_report_contract_balances,
    parse_report_opening_balances,
)


def test_parse_report_opening_balances_reads_top_level_counterparty_rows(tmp_path: Path) -> None:
    report = tmp_path / "report.txt"
    report.write_text(
        "\n".join(
            [
                "\tКонтрагент\tСумма взаиморасчетов",
                "",
                "\tАннамурадов Владислав\t90\xa0827,00\t1\xa0483,00\t84\xa0345,00\t7\xa0965,00",
                "\tдоговор займа\t90\xa0000,00\t\t84\xa0000,00\t6\xa0000,00",
                "\tОсновной договор\t827,00\t1\xa0483,00\t345,00\t1\xa0965,00",
                "",
                "\tКопьев Михаил Андреевич\t7\xa0160,00\t5\xa0674,00\t12\xa0834,00\t",
                "\tОсновной договор\t7\xa0160,00\t5\xa0674,00\t12\xa0834,00\t",
                "",
                "\tИтог\t97\xa0987,00\t7\xa0157,00\t97\xa0179,00\t7\xa0965,00",
            ]
        ),
        encoding="utf-8",
    )

    result = parse_report_opening_balances(report)

    assert result == {
        "Аннамурадов Владислав": Decimal("90827"),
        "Копьев Михаил Андреевич": Decimal("7160"),
    }


def test_filter_ledger_events_by_contract_kind_and_source_layer() -> None:
    base = dict(
        source="onec",
        event_type="opening_balance",
        external_document_number=None,
        external_document_date=datetime(2025, 1, 1, 0, 0, 0),
        counterparty_ref="cp-1",
        counterparty_name="Контрагент 1",
        contract_ref="contract-1",
        contract_name="Основной договор",
        manager_ref=None,
        manager_name=None,
        store_ref=None,
        store_name=None,
        planned_payment_date=None,
        credit_depth_days=None,
        shipment_ban=None,
        line_no=1,
        amount_delta=Decimal("10"),
    )
    events = [
        ReceivableLedgerRow(
            external_document_ref="doc-1",
            contract_kind_ref="kind-buyer",
            contract_kind_name="С покупателем",
            source_layer="employee_summary",
            **base,
        ),
        ReceivableLedgerRow(
            external_document_ref="doc-2",
            contract_kind_ref="kind-supplier",
            contract_kind_name="С поставщиком",
            source_layer="employee_summary",
            **base,
        ),
        ReceivableLedgerRow(
            external_document_ref="doc-3",
            contract_kind_ref="kind-buyer",
            contract_kind_name="С покупателем",
            source_layer="regular_receivables",
            **base,
        ),
    ]

    result = filter_ledger_events(
        events,
        contract_kind_names={"С покупателем"},
        source_layer="employee_summary",
    )

    assert [item.external_document_ref for item in result] == ["doc-1"]


def test_parse_report_contract_balances_reads_contract_rows(tmp_path: Path) -> None:
    report = tmp_path / "report.txt"
    report.write_text(
        "\n".join(
            [
                "\tКонтрагент\tСумма взаиморасчетов",
                "",
                "\tАннамурадов Владислав\t90\xa0827,00\t1\xa0483,00\t84\xa0345,00\t7\xa0965,00",
                "\tдоговор займа\t90\xa0000,00\t\t84\xa0000,00\t6\xa0000,00",
                "\tОсновной договор\t827,00\t1\xa0483,00\t345,00\t1\xa0965,00",
                "",
                "\tКопьев Михаил Андреевич\t7\xa0160,00\t5\xa0674,00\t12\xa0834,00\t",
                "\tОсновной договор\t7\xa0160,00\t5\xa0674,00\t12\xa0834,00\t",
            ]
        ),
        encoding="utf-8",
    )

    result = parse_report_contract_balances(report)

    assert result == {
        "Аннамурадов Владислав": [
            ("договор займа", Decimal("90000")),
            ("Основной договор", Decimal("827")),
        ],
        "Копьев Михаил Андреевич": [
            ("Основной договор", Decimal("7160")),
        ],
    }


def test_onec_engine_scope_uses_bounded_factory_and_disposes_on_error(monkeypatch) -> None:
    calls: list[tuple[str, int, int]] = []

    class FakeEngine:
        disposed = False

        def dispose(self) -> None:
            self.disposed = True

    engine = FakeEngine()

    def fake_build_onec_engine(
        database_url: str,
        *,
        query_timeout_seconds: int,
        login_timeout_seconds: int,
    ) -> FakeEngine:
        calls.append((database_url, query_timeout_seconds, login_timeout_seconds))
        return engine

    monkeypatch.setattr(report_task, "build_onec_engine", fake_build_onec_engine)

    with pytest.raises(RuntimeError, match="query failed"):
        with report_task._onec_engine_scope(
            "mssql+pyodbc://onec",
            query_timeout_seconds=45,
            login_timeout_seconds=7,
        ) as current_engine:
            assert current_engine is engine
            raise RuntimeError("query failed")

    assert calls == [("mssql+pyodbc://onec", 45, 7)]
    assert engine.disposed is True


def test_build_temp_snapshots_keeps_writes_inside_temporary_sqlite_scope(
    monkeypatch,
) -> None:
    onec_engine = object()

    @contextmanager
    def fake_onec_engine_scope(*_args, **_kwargs):
        yield onec_engine

    class FakeExtractor:
        def __init__(self, current_engine, *, operations_sql: str) -> None:
            assert current_engine is onec_engine
            assert operations_sql == "SELECT 1"

        def fetch_receivable_events(self, **_kwargs):
            return []

    monkeypatch.setattr(report_task, "_onec_engine_scope", fake_onec_engine_scope)
    monkeypatch.setattr(
        report_task,
        "fetch_employee_counterparty_refs_from_onec",
        lambda current_engine: [] if current_engine is onec_engine else None,
    )
    monkeypatch.setattr(
        report_task,
        "fetch_staff_members_from_onec",
        lambda current_engine: [] if current_engine is onec_engine else None,
    )
    monkeypatch.setattr(report_task, "OneCReceivableLedgerExtractor", FakeExtractor)

    result = report_task.build_temp_snapshots(
        operations_sql="SELECT 1",
        opening_balance_date=date(2025, 1, 1),
        window_start=datetime(2025, 1, 1),
        window_end=None,
        snapshot_date=None,
        onec_url="sqlite:///:memory:",
        onec_query_timeout_seconds=30,
        onec_login_timeout_seconds=5,
    )

    assert result == ({}, {}, {})


def test_main_preserves_cli_filters_and_tsv_contract(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    sql_path = tmp_path / "receivables.sql"
    report_path = tmp_path / "report.txt"
    sql_path.write_text("SELECT 1", encoding="utf-8")
    report_path.write_text("placeholder", encoding="utf-8")
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        report_task,
        "get_settings",
        lambda: SimpleNamespace(
            onec_database_url="mssql+pyodbc://settings",
            onec_query_timeout_seconds=41,
            onec_login_timeout_seconds=13,
        ),
    )
    monkeypatch.setattr(
        report_task,
        "parse_report_opening_balances",
        lambda _path: {"Employee": Decimal("100")},
    )

    def fake_build_temp_snapshots(**kwargs):
        calls.append(kwargs)
        return (
            {"Employee": Decimal("90")},
            {"Employee": Decimal("80")},
            {},
        )

    monkeypatch.setattr(report_task, "build_temp_snapshots", fake_build_temp_snapshots)
    monkeypatch.setattr(
        "sys.argv",
        [
            "compare_employee_receivable_report",
            "--sql-file",
            str(sql_path),
            "--report-file",
            str(report_path),
            "--opening-balance-date",
            "2025-01-01",
            "--window-start",
            "2025-01-02T03:04:05",
            "--snapshot-date",
            "2025-01-03",
            "--name",
            "Employee",
            "--contract-kind-name",
            "С покупателем",
            "--source-layer",
            "employee_summary",
            "--onec-url",
            "mssql+pyodbc://override",
        ],
    )

    report_task.main()

    assert calls == [
        {
            "operations_sql": "SELECT 1",
            "opening_balance_date": date(2025, 1, 1),
            "window_start": datetime(2025, 1, 2, 3, 4, 5),
            "window_end": None,
            "snapshot_date": date(2025, 1, 3),
            "onec_url": "mssql+pyodbc://override",
            "onec_query_timeout_seconds": 41,
            "onec_login_timeout_seconds": 13,
            "contract_kind_names": {"С покупателем"},
            "source_layer": "employee_summary",
        }
    ]
    assert capsys.readouterr().out.splitlines() == [
        "name\treport_opening\tsql_opening\tdiff\tsnapshot_balance",
        "Employee\t100\t90\t10\t80",
    ]
