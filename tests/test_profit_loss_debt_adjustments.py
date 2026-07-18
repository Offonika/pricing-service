from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.models.executive_dashboard import ExecutiveSourceFreshness
from app.models.receivable_ledger_event import ReceivableLedgerEvent
from app.services.profit_loss_debt_adjustments import (
    ONEC_DEBT_WRITEOFF_SQL,
    PROFIT_LOSS_DEBT_ADJUSTMENT_SOURCE_LAYER,
    classify_debt_writeoff_rows,
    publish_debt_writeoff_batch,
)
from tasks.publish_profit_loss_debt_adjustments import _month_bounds


def _source_row(
    *,
    document_ref: str,
    line_no: int,
    debt_type_order: int | None,
    amount: str,
) -> dict[str, object]:
    return {
        "external_document_ref": document_ref,
        "external_document_number": f"КД-{line_no}",
        "external_document_date": datetime(2026, 6, 15, 12, 0),
        "line_no": line_no,
        "debt_type_order": debt_type_order,
        "amount": amount,
        "contract_ref": f"0xcontract{line_no}",
        "contract_name": "Основной договор",
        "contract_kind_ref": "0xkind",
        "contract_kind_name": "С покупателем",
        "counterparty_ref": f"0xcounterparty{line_no}",
        "counterparty_name": "Контрагент",
        "organization_ref": "0xorganization",
        "organization_name": "MASTER MOBILE",
    }


def test_classification_maps_receivable_to_expense_and_payable_to_income() -> None:
    batch = classify_debt_writeoff_rows(
        [
            _source_row(
                document_ref="0xdoc-expense",
                line_no=1,
                debt_type_order=0,
                amount="27718.49",
            ),
            _source_row(
                document_ref="0xdoc-income",
                line_no=2,
                debt_type_order=1,
                amount="111564.10",
            ),
        ]
    )

    assert batch.source_status == "ready"
    assert batch.expense == Decimal("27718.49")
    assert batch.income == Decimal("111564.10")
    assert [record.amount_delta for record in batch.records] == [
        Decimal("-27718.49"),
        Decimal("111564.10"),
    ]
    assert len(batch.content_sha256) == 64


def test_classification_marks_unknown_invalid_and_duplicate_rows_partial() -> None:
    valid = _source_row(
        document_ref="0xdoc",
        line_no=1,
        debt_type_order=0,
        amount="100.00",
    )
    unknown = _source_row(
        document_ref="0xunknown",
        line_no=2,
        debt_type_order=None,
        amount="50.00",
    )
    invalid = _source_row(
        document_ref="0xinvalid",
        line_no=3,
        debt_type_order=1,
        amount="0",
    )

    batch = classify_debt_writeoff_rows([valid, valid, unknown, invalid])

    assert batch.source_status == "partial"
    assert len(batch.records) == 1
    assert batch.rejected_count == 3
    assert batch.rejection_reasons == {
        "duplicate_business_key": 1,
        "unknown_debt_type": 1,
        "invalid_amount": 1,
    }


def test_publish_replaces_only_selected_month_and_updates_freshness() -> None:
    engine = create_engine("sqlite:///:memory:")
    ReceivableLedgerEvent.__table__.create(engine)
    ExecutiveSourceFreshness.__table__.create(engine)
    june_batch = classify_debt_writeoff_rows(
        [
            _source_row(
                document_ref="0xjune-old",
                line_no=1,
                debt_type_order=0,
                amount="10.00",
            )
        ]
    )
    july_row = _source_row(
        document_ref="0xjuly",
        line_no=1,
        debt_type_order=1,
        amount="20.00",
    )
    july_row["external_document_date"] = datetime(2026, 7, 15, 12, 0)
    july_batch = classify_debt_writeoff_rows([july_row])
    replacement_batch = classify_debt_writeoff_rows(
        [
            _source_row(
                document_ref="0xjune-new",
                line_no=2,
                debt_type_order=1,
                amount="30.00",
            )
        ]
    )

    with Session(engine) as session:
        publish_debt_writeoff_batch(
            session,
            batch=june_batch,
            period_start=date(2026, 6, 1),
            period_end=date(2026, 6, 30),
            source_as_of=datetime(2026, 7, 17, 10, 0),
        )
        publish_debt_writeoff_batch(
            session,
            batch=july_batch,
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 31),
            source_as_of=datetime(2026, 8, 1, 10, 0),
        )
        publish_debt_writeoff_batch(
            session,
            batch=replacement_batch,
            period_start=date(2026, 6, 1),
            period_end=date(2026, 6, 30),
            source_as_of=datetime(2026, 7, 17, 11, 0),
        )
        session.commit()

        events = list(
            session.scalars(
                select(ReceivableLedgerEvent).order_by(ReceivableLedgerEvent.external_document_date)
            )
        )
        june_publication = session.scalar(
            select(ExecutiveSourceFreshness).where(
                ExecutiveSourceFreshness.business_date == date(2026, 6, 30)
            )
        )
        publication_count = session.scalar(select(func.count(ExecutiveSourceFreshness.id)))

    assert [event.external_document_ref for event in events] == ["0xjune-new", "0xjuly"]
    assert [event.source_layer for event in events] == [
        PROFIT_LOSS_DEBT_ADJUSTMENT_SOURCE_LAYER,
        PROFIT_LOSS_DEBT_ADJUSTMENT_SOURCE_LAYER,
    ]
    assert june_publication is not None
    assert june_publication.source_status == "ready"
    assert june_publication.payload["income_amount"] == "30.00"
    assert june_publication.payload["expense_amount"] == "0.00"
    assert publication_count == 2


def test_onec_query_accepts_only_posted_writeoff_documents_for_target_organization() -> None:
    compact_sql = " ".join(ONEC_DEBT_WRITEOFF_SQL.split())

    assert "operation._EnumOrder = 2" in compact_sql
    assert "organization._Description = :organization_name" in compact_sql
    assert "doc._Marked = 0x00" in compact_sql
    assert "doc._Posted = 0x01" in compact_sql
    assert "_Document160_VT3169" in compact_sql
    assert "debt_type._EnumOrder" in compact_sql


def test_month_bounds_uses_calendar_month() -> None:
    assert _month_bounds("2026-06") == (date(2026, 6, 1), date(2026, 6, 30))
    assert _month_bounds("2024-02") == (date(2024, 2, 1), date(2024, 2, 29))
