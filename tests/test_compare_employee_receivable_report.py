from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

from app.services.receivables import ReceivableLedgerRow
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
