from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.dependencies import get_db
from app.core.config import get_settings
from app.main import app
from app.models import Base
from app.models.counterparty_folder_snapshot import CounterpartyFolderSnapshot
from app.models.receivable_balance_snapshot import ReceivableBalanceSnapshot
from app.models.receivable_ledger_event import ReceivableLedgerEvent
from app.services.counterparty_folder_recommendations import (
    OPEN_DEBT_DIAGNOSTIC_MATCHED,
    OPEN_DEBT_DIAGNOSTIC_STATEMENT_MISSING,
    OPEN_DEBT_DIAGNOSTIC_STRUCTURE_UNCONFIRMED,
    OPEN_DEBT_DIAGNOSTIC_TOTAL_ABOVE_BALANCE,
    OPEN_DEBT_DIAGNOSTIC_TOTAL_BELOW_BALANCE,
    QUEUE_BUSINESS_REVIEW,
    QUEUE_EXCLUDED,
    STATUS_MOVE_RECOMMENDED,
    STATUS_NEEDS_REVIEW,
    STATUS_NO_OVERDUE,
    STATUS_OK,
    CounterpartyFolderRow,
    SaleDocumentDepartmentRow,
    _apply_document_mismatch_guard,
    _apply_report_suppression,
    _build_item,
    build_counterparty_folder_recommendations,
    classify_open_debt_documents,
    enrich_folder_recommendation_item,
    open_debt_documents_match_balance,
)
from app.services.counterparty_folder_snapshots import (
    build_counterparty_folder_changes,
    sync_counterparty_folder_snapshot,
)
from app.services.receivable_canonical_debt_origin import (
    CANONICAL_DEBT_STATUS_BALANCE_MISMATCH,
    CANONICAL_DEBT_STATUS_MATCHED,
    CanonicalDebtSaleCandidate,
    resolve_canonical_debt_origin,
)
from app.services.receivable_statement_debt import (
    ReceivableStatementEvent,
    resolve_open_debt_documents_by_statement,
)

SNAPSHOT_DATE = date(2026, 5, 29)


def _statement_event(
    event_type: str,
    ref: str,
    number: str,
    dt: datetime,
    amount: str,
    *,
    line_no: int | None = None,
) -> ReceivableStatementEvent:
    return ReceivableStatementEvent(
        counterparty_ref="cp-statement",
        event_type=event_type,
        document_ref=ref,
        document_number=number,
        document_date=dt,
        amount_delta=Decimal(amount),
        line_no=line_no,
    )


def _make_sqlite_engine(path: str | None = None):
    if path is not None:
        return create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    return create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


def _override_db(engine):
    def _override():
        db = Session(engine)
        try:
            yield db
        finally:
            db.close()

    return _override


def _snapshot(
    counterparty_ref: str,
    *,
    counterparty_name: str,
    balance: str,
    document_ref: str | None,
    document_number: str | None,
    document_date: datetime | None,
    credit_depth_days: int | None,
    is_overdue: bool,
    overdue_days: int | None,
) -> ReceivableBalanceSnapshot:
    due_date = (
        document_date + timedelta(days=credit_depth_days)
        if document_date is not None and credit_depth_days is not None
        else None
    )
    return ReceivableBalanceSnapshot(
        snapshot_date=SNAPSHOT_DATE,
        counterparty_ref=counterparty_ref,
        counterparty_name=counterparty_name,
        current_balance=Decimal(balance),
        origin_document_ref=document_ref,
        origin_document_number=document_number,
        origin_document_date=document_date,
        origin_manager_ref="mgr-origin",
        origin_manager_name="Менеджер долга",
        current_manager_ref="mgr-current",
        current_manager_name="Текущий менеджер",
        planned_payment_date=None,
        credit_depth_days=credit_depth_days,
        shipment_ban=False,
        payment_term_source="credit_depth_days" if credit_depth_days is not None else "missing",
        due_date=due_date,
        overdue_days=overdue_days,
        is_overdue=is_overdue,
        aged_bucket="31+",
        activity_segment="active",
    )


def _ledger_sale(
    counterparty_ref: str,
    *,
    document_ref: str,
    document_number: str,
    document_date: datetime,
    amount: str,
) -> ReceivableLedgerEvent:
    return ReceivableLedgerEvent(
        source="test",
        business_key=f"{counterparty_ref}:{document_ref}",
        event_type="sale",
        external_document_ref=document_ref,
        external_document_number=document_number,
        external_document_date=document_date,
        counterparty_ref=counterparty_ref,
        counterparty_name=counterparty_ref,
        manager_ref="mgr-origin",
        manager_name="Менеджер долга",
        source_layer="regular_receivables",
        amount_delta=Decimal(amount),
    )


def test_statement_debt_resolver_closes_sale_by_nearby_payment() -> None:
    docs = resolve_open_debt_documents_by_statement(
        [
            _statement_event("sale", "sale-1", "РТУ-1", datetime(2026, 6, 1, 10), "1000.00"),
            _statement_event(
                "payment",
                "pko-1",
                "ПКО-1",
                datetime(2026, 6, 1, 11),
                "-1049.00",
            ),
            _statement_event("sale", "sale-2", "РТУ-2", datetime(2026, 6, 2, 10), "700.00"),
        ],
        current_balance=Decimal("700.00"),
    )

    assert [item.document_number for item in docs] == ["РТУ-2"]


def test_statement_debt_resolver_closes_sale_by_return_adjusted_payment() -> None:
    docs = resolve_open_debt_documents_by_statement(
        [
            _statement_event("sale", "sale-1", "РТУ-1", datetime(2026, 6, 1, 10), "1000.00"),
            _statement_event(
                "return",
                "return-1",
                "ВОЗВ-1",
                datetime(2026, 6, 1, 10, 30),
                "-250.00",
            ),
            _statement_event(
                "payment",
                "pko-1",
                "ПКО-1",
                datetime(2026, 6, 1, 11),
                "-749.00",
            ),
            _statement_event("sale", "sale-2", "РТУ-2", datetime(2026, 6, 2, 10), "900.00"),
        ],
        current_balance=Decimal("900.00"),
    )

    assert [item.document_number for item in docs] == ["РТУ-2"]


def test_statement_debt_resolver_closes_multiple_sales_by_one_payment() -> None:
    docs = resolve_open_debt_documents_by_statement(
        [
            _statement_event("sale", "sale-1", "РТУ-1", datetime(2026, 6, 1, 10), "400.00"),
            _statement_event("sale", "sale-2", "РТУ-2", datetime(2026, 6, 1, 11), "600.00"),
            _statement_event(
                "payment",
                "pko-1",
                "ПКО-1",
                datetime(2026, 6, 1, 12),
                "-1090.00",
            ),
            _statement_event("sale", "sale-3", "РТУ-3", datetime(2026, 6, 2, 10), "500.00"),
        ],
        current_balance=Decimal("500.00"),
    )

    assert [item.document_number for item in docs] == ["РТУ-3"]


def test_statement_debt_resolver_skips_intermediate_non_matching_payment() -> None:
    docs = resolve_open_debt_documents_by_statement(
        [
            _statement_event("sale", "sale-1", "РТУ-1", datetime(2026, 6, 1, 10), "1000.00"),
            _statement_event(
                "payment",
                "pko-noise",
                "ПКО-NOISE",
                datetime(2026, 6, 1, 10, 30),
                "-300.00",
            ),
            _statement_event(
                "payment",
                "pko-1",
                "ПКО-1",
                datetime(2026, 6, 1, 11),
                "-1000.00",
            ),
            _statement_event("sale", "sale-2", "РТУ-2", datetime(2026, 6, 2, 10), "800.00"),
        ],
        current_balance=Decimal("800.00"),
    )

    assert [item.document_number for item in docs] == ["РТУ-2"]


def test_statement_debt_resolver_uses_bottom_up_balance_cutoff() -> None:
    docs = resolve_open_debt_documents_by_statement(
        [
            _statement_event("sale", "sale-1", "РТУ-1", datetime(2026, 6, 1, 10), "4000.00"),
            _statement_event("sale", "sale-2", "РТУ-2", datetime(2026, 6, 2, 10), "6000.00"),
            _statement_event("sale", "sale-3", "РТУ-3", datetime(2026, 6, 3, 10), "7000.00"),
        ],
        current_balance=Decimal("9000.00"),
    )

    assert [item.document_number for item in docs] == ["РТУ-2", "РТУ-3"]
    assert docs[0].statement_selection_rule == "statement_bottom_up_balance_cutoff"
    assert docs[0].open_amount == Decimal("2000.00")


def test_statement_debt_resolver_uses_last_safe_balance_segment() -> None:
    docs = resolve_open_debt_documents_by_statement(
        [
            _statement_event("sale", "sale-1", "РТУ-1", datetime(2026, 6, 1, 10), "1000.00"),
            _statement_event("payment", "pko-1", "ПКО-1", datetime(2026, 6, 1, 11), "-1000.00"),
            _statement_event("sale", "sale-2", "РТУ-2", datetime(2026, 6, 2, 10), "700.00"),
        ],
        current_balance=Decimal("700.00"),
    )

    assert [item.document_number for item in docs] == ["РТУ-2"]
    assert docs[0].statement_balance_after == Decimal("700.00")
    assert docs[0].statement_segment_start_row == 3
    assert docs[0].statement_segment_end_row == 3


def test_statement_debt_resolver_does_not_cut_segment_when_payment_belongs_to_next_sale() -> None:
    docs = resolve_open_debt_documents_by_statement(
        [
            _statement_event("sale", "sale-1", "РТУ-1", datetime(2026, 6, 1, 10), "1000.00"),
            _statement_event("payment", "pko-1", "ПКО-1", datetime(2026, 6, 1, 11), "-1000.00"),
            _statement_event("sale", "sale-2", "РТУ-2", datetime(2026, 6, 1, 12), "1000.00"),
        ],
        current_balance=Decimal("1000.00"),
    )

    assert [item.document_number for item in docs] == ["РТУ-2"]
    assert docs[0].statement_segment_start_row == 1


def test_statement_debt_resolver_does_not_double_apply_structure_linked_payment() -> None:
    docs = resolve_open_debt_documents_by_statement(
        [
            _statement_event("sale", "sale-1", "РТУ-1", datetime(2026, 6, 1, 10), "1000.00"),
            _statement_event(
                "payment",
                "pko-1",
                "ПКО-1",
                datetime(2026, 6, 1, 11),
                "-500.00",
            ),
        ],
        current_balance=Decimal("500.00"),
        structure_checks={
            "sale-1": SimpleNamespace(
                status="confirmed_open",
                open_amount=Decimal("500.00"),
                closing_amount=Decimal("-500.00"),
                linked_documents=(
                    {
                        "document_ref": "pko-1",
                        "document_number": "ПКО-1",
                        "amount": Decimal("-500.00"),
                    },
                ),
            )
        },
    )

    assert [item.document_number for item in docs] == ["РТУ-1"]
    assert docs[0].open_amount == Decimal("500.00")


def test_statement_debt_resolver_processes_multiple_grouped_payments() -> None:
    docs = resolve_open_debt_documents_by_statement(
        [
            _statement_event("sale", "sale-1", "РТУ-1", datetime(2026, 6, 1, 10), "400.00"),
            _statement_event("sale", "sale-2", "РТУ-2", datetime(2026, 6, 1, 11), "600.00"),
            _statement_event("payment", "pko-1", "ПКО-1", datetime(2026, 6, 1, 12), "-1000.00"),
            _statement_event("sale", "sale-3", "РТУ-3", datetime(2026, 6, 2, 10), "300.00"),
            _statement_event("sale", "sale-4", "РТУ-4", datetime(2026, 6, 2, 11), "400.00"),
            _statement_event("payment", "pko-2", "ПКО-2", datetime(2026, 6, 2, 12), "-700.00"),
            _statement_event("sale", "sale-5", "РТУ-5", datetime(2026, 6, 3, 10), "900.00"),
            _statement_event("sale", "sale-6", "РТУ-6", datetime(2026, 6, 3, 11), "800.00"),
        ],
        current_balance=Decimal("2400.00"),
    )

    assert [item.document_number for item in docs] == ["РТУ-5", "РТУ-6"]


def test_statement_debt_resolver_limits_multi_sale_matching_to_payment_window() -> None:
    historical_events = [
        _statement_event(
            "sale",
            f"old-sale-{index}",
            f"СТАР-{index}",
            datetime(2026, 1, 1, 10) + timedelta(minutes=index),
            "1.00",
        )
        for index in range(500)
    ]
    docs = resolve_open_debt_documents_by_statement(
        [
            *historical_events,
            _statement_event("sale", "sale-1", "РТУ-1", datetime(2026, 6, 1, 10), "400.00"),
            _statement_event("sale", "sale-2", "РТУ-2", datetime(2026, 6, 1, 11), "600.00"),
            _statement_event("payment", "pko-1", "ПКО-1", datetime(2026, 6, 1, 12), "-1000.00"),
            _statement_event("sale", "sale-3", "РТУ-3", datetime(2026, 6, 2, 10), "900.00"),
        ],
        current_balance=Decimal("1400.00"),
    )

    document_numbers = [item.document_number for item in docs]
    assert "РТУ-1" not in document_numbers
    assert "РТУ-2" not in document_numbers
    assert "РТУ-3" in document_numbers


def test_statement_debt_resolver_does_not_group_match_far_sales() -> None:
    docs = resolve_open_debt_documents_by_statement(
        [
            _statement_event("sale", "sale-1", "РТУ-1", datetime(2026, 6, 1, 10), "400.00"),
            _statement_event("sale", "sale-2", "РТУ-2", datetime(2026, 6, 1, 11), "600.00"),
            _statement_event("sale", "noise-1", "ШУМ-1", datetime(2026, 6, 1, 12), "1.00"),
            _statement_event("sale", "noise-2", "ШУМ-2", datetime(2026, 6, 1, 13), "1.00"),
            _statement_event("sale", "noise-3", "ШУМ-3", datetime(2026, 6, 1, 14), "1.00"),
            _statement_event("sale", "noise-4", "ШУМ-4", datetime(2026, 6, 1, 15), "1.00"),
            _statement_event("sale", "noise-5", "ШУМ-5", datetime(2026, 6, 1, 16), "1.00"),
            _statement_event("sale", "noise-6", "ШУМ-6", datetime(2026, 6, 1, 17), "1.00"),
            _statement_event("sale", "noise-7", "ШУМ-7", datetime(2026, 6, 1, 18), "1.00"),
            _statement_event("sale", "noise-8", "ШУМ-8", datetime(2026, 6, 1, 19), "1.00"),
            _statement_event("payment", "pko-1", "ПКО-1", datetime(2026, 6, 1, 20), "-900.00"),
        ],
        current_balance=Decimal("1008.00"),
    )

    assert [item.document_number for item in docs][:2] == ["РТУ-1", "РТУ-2"]


def test_statement_debt_resolver_caps_single_large_bottom_up_sale_to_balance() -> None:
    docs = resolve_open_debt_documents_by_statement(
        [
            _statement_event("sale", "sale-1", "РТУ-1", datetime(2026, 6, 1, 10), "10000.00"),
        ],
        current_balance=Decimal("3000.00"),
    )

    assert [item.document_number for item in docs] == ["РТУ-1"]
    assert docs[0].statement_selection_rule == "statement_bottom_up_balance_cutoff"
    assert docs[0].open_amount == Decimal("3000.00")


def test_statement_debt_resolver_prefers_old_structure_confirmed_debt() -> None:
    docs = resolve_open_debt_documents_by_statement(
        [
            _statement_event("sale", "sale-2025", "РТУ-2025", datetime(2025, 8, 1), "1000"),
            _statement_event("sale", "sale-2026", "РТУ-2026", datetime(2026, 7, 1), "1000"),
        ],
        current_balance=Decimal("1000.00"),
        structure_checks={
            "sale-2025": SimpleNamespace(
                status="confirmed_open",
                open_amount=Decimal("1000.00"),
                closing_amount=Decimal("0.00"),
                linked_documents=(),
            )
        },
    )

    assert [item.document_number for item in docs] == ["РТУ-2025"]
    assert docs[0].statement_selection_rule == "statement_structure_confirmed_open"


def test_statement_debt_resolver_keeps_confirmed_debt_before_later_zero_balance() -> None:
    docs = resolve_open_debt_documents_by_statement(
        [
            _statement_event("sale", "sale-2025", "РТУ-2025", datetime(2025, 8, 1), "1000"),
            _statement_event("payment", "payment-2025", "ПКО-2025", datetime(2025, 8, 2), "-1000"),
            _statement_event("sale", "sale-2026", "РТУ-2026", datetime(2026, 7, 1), "1000"),
        ],
        current_balance=Decimal("1000.00"),
        structure_checks={
            "sale-2025": SimpleNamespace(
                status="confirmed_open",
                open_amount=Decimal("1000.00"),
                closing_amount=Decimal("0.00"),
                linked_documents=(),
            )
        },
    )

    assert [item.document_number for item in docs] == ["РТУ-2025"]
    assert docs[0].open_amount == Decimal("1000.00")
    assert docs[0].statement_selection_rule == "statement_structure_confirmed_open"


def test_statement_debt_resolver_sorts_confirmed_open_documents_oldest_first() -> None:
    docs = resolve_open_debt_documents_by_statement(
        [
            _statement_event("sale", "sale-new", "РТУ-НОВАЯ", datetime(2026, 7, 1), "600"),
            _statement_event("sale", "sale-old", "РТУ-СТАРАЯ", datetime(2025, 8, 1), "400"),
        ],
        current_balance=Decimal("1000.00"),
        structure_checks={
            ref: SimpleNamespace(
                status="confirmed_open",
                open_amount=amount,
                closing_amount=Decimal("0.00"),
                linked_documents=(),
            )
            for ref, amount in {
                "sale-new": Decimal("600.00"),
                "sale-old": Decimal("400.00"),
            }.items()
        },
    )

    assert [item.document_number for item in docs] == ["РТУ-СТАРАЯ", "РТУ-НОВАЯ"]
    assert [item.open_amount for item in docs] == [Decimal("400.00"), Decimal("600.00")]


def test_open_debt_diagnostics_do_not_accept_missing_or_mismatched_documents() -> None:
    assert not open_debt_documents_match_balance([], current_balance=Decimal("100.00"))
    assert (
        classify_open_debt_documents([], current_balance=Decimal("100.00"), statement_sale_count=0)
        == OPEN_DEBT_DIAGNOSTIC_STATEMENT_MISSING
    )
    assert (
        classify_open_debt_documents([], current_balance=Decimal("100.00"), statement_sale_count=1)
        == OPEN_DEBT_DIAGNOSTIC_STRUCTURE_UNCONFIRMED
    )
    confirmed = {
        "open_amount": "100.00",
        "document_structure_status": "confirmed_open",
    }
    assert (
        classify_open_debt_documents(
            [confirmed], current_balance=Decimal("100.00"), statement_sale_count=1
        )
        == OPEN_DEBT_DIAGNOSTIC_MATCHED
    )
    assert (
        classify_open_debt_documents(
            [{**confirmed, "open_amount": "90.00"}],
            current_balance=Decimal("100.00"),
            statement_sale_count=1,
        )
        == OPEN_DEBT_DIAGNOSTIC_TOTAL_BELOW_BALANCE
    )
    assert (
        classify_open_debt_documents(
            [{**confirmed, "open_amount": "110.00"}],
            current_balance=Decimal("100.00"),
            statement_sale_count=1,
        )
        == OPEN_DEBT_DIAGNOSTIC_TOTAL_ABOVE_BALANCE
    )
    assert (
        classify_open_debt_documents(
            [{**confirmed, "open_amount": "100.01"}],
            current_balance=Decimal("100.00"),
            statement_sale_count=1,
        )
        == OPEN_DEBT_DIAGNOSTIC_MATCHED
    )
    assert (
        classify_open_debt_documents(
            [{**confirmed, "open_amount": "100.02"}],
            current_balance=Decimal("100.00"),
            statement_sale_count=1,
        )
        == OPEN_DEBT_DIAGNOSTIC_TOTAL_ABOVE_BALANCE
    )


def test_document_mismatch_guard_clears_unverified_terms_and_preserves_diagnostics() -> None:
    snapshot = _snapshot(
        "cp-mismatch",
        counterparty_name="Свежий долг после закрытой накладной",
        balance="11960.00",
        document_ref="doc-closed",
        document_number="РТУ-СТАРАЯ",
        document_date=datetime(2026, 7, 10, 10, 0),
        credit_depth_days=7,
        is_overdue=True,
        overdue_days=14,
    )
    item = _build_item(
        snapshot,
        folder_row=None,
        document_row=None,
        open_debt_documents=[],
    )

    guarded = _apply_document_mismatch_guard(
        item,
        diagnostic=OPEN_DEBT_DIAGNOSTIC_TOTAL_BELOW_BALANCE,
    )

    assert guarded["current_balance"] == Decimal("11960.00")
    assert guarded["open_debt_source_status"] == "document_mismatch"
    assert guarded["document_mismatch_reason"] == "open_debt_document_total_below_balance"
    assert guarded["review_reason"] == "open_debt_document_total_below_balance"
    assert guarded["status"] == STATUS_NEEDS_REVIEW
    assert guarded["origin_document_ref"] is None
    assert guarded["origin_document_number"] is None
    assert guarded["origin_document_date"] is None
    assert guarded["due_date"] is None
    assert guarded["overdue_days"] is None
    assert guarded["effective_due_date"] is None
    assert guarded["effective_overdue_days"] is None
    assert guarded["is_overdue"] is False
    assert guarded["open_debt_documents"] == []
    assert guarded["recommended_folder_ref"] is None


def test_below_minimum_suppression_keeps_document_mismatch_reason() -> None:
    item = _apply_document_mismatch_guard(
        {
            "current_balance": Decimal("10.00"),
            "status": STATUS_NEEDS_REVIEW,
            "review_reason": "open_structure_document_not_found",
        },
        diagnostic=OPEN_DEBT_DIAGNOSTIC_STATEMENT_MISSING,
    )

    suppressed = _apply_report_suppression(item)

    assert suppressed["status"] == STATUS_NO_OVERDUE
    assert suppressed["review_reason"] == "open_debt_statement_missing"
    assert suppressed["suppressed_from_daily_report"] is True
    assert suppressed["suppression_reason"] == "below_min_balance_threshold"


def test_below_minimum_suppression_marks_non_actionable_item_for_daily_filter() -> None:
    suppressed = _apply_report_suppression(
        {
            "current_balance": Decimal("90.00"),
            "status": STATUS_OK,
            "review_reason": None,
        }
    )

    assert suppressed["status"] == STATUS_OK
    assert suppressed["review_reason"] is None
    assert suppressed["suppressed_from_daily_report"] is True
    assert suppressed["suppression_reason"] == "below_min_balance_threshold"


def test_exclusion_reason_survives_document_diagnostics() -> None:
    snapshot = _snapshot(
        "cp-service",
        counterparty_name="Служебный контрагент",
        balance="1000.00",
        document_ref="doc-service",
        document_number="РТУ-СЛУЖЕБНАЯ",
        document_date=datetime(2026, 5, 1),
        credit_depth_days=7,
        is_overdue=True,
        overdue_days=20,
    )
    item = _build_item(
        snapshot,
        folder_row=CounterpartyFolderRow(
            counterparty_ref="cp-service",
            counterparty_code="РБ034645",
            counterparty_name="Служебный контрагент",
            current_folder_ref="folder-main",
            current_folder_name="01. Горбушка",
        ),
        document_row=None,
        open_debt_documents=[],
    )
    guarded = _apply_document_mismatch_guard(
        item,
        diagnostic=OPEN_DEBT_DIAGNOSTIC_TOTAL_BELOW_BALANCE,
    )
    enriched = enrich_folder_recommendation_item(guarded)

    assert enriched["exclusion_reason"] == "excluded_service_counterparty"
    assert enriched["review_reason"] == "open_debt_document_total_below_balance"
    assert enriched["queue"] == QUEUE_EXCLUDED


def test_supplier_folder_is_excluded_for_current_or_recommended_folder() -> None:
    snapshot = _snapshot(
        "cp-supplier",
        counterparty_name="Поставщик",
        balance="1000.00",
        document_ref="doc-supplier",
        document_number="РТУ-ПОСТАВЩИК",
        document_date=datetime(2026, 5, 1),
        credit_depth_days=7,
        is_overdue=True,
        overdue_days=20,
    )
    current_supplier = _build_item(
        snapshot,
        folder_row=CounterpartyFolderRow(
            counterparty_ref="cp-supplier",
            counterparty_code="РБ048956",
            counterparty_name="Поставщик",
            current_folder_ref="folder-suppliers",
            current_folder_name="  ПОСТАВЩИКИ  ",
        ),
        document_row=None,
        open_debt_documents=[],
    )
    recommended_supplier = _build_item(
        snapshot,
        folder_row=CounterpartyFolderRow(
            counterparty_ref="cp-supplier",
            counterparty_code="РБ048956",
            counterparty_name="Поставщик",
            current_folder_ref="folder-main",
            current_folder_name="01. Горбушка",
        ),
        document_row=SaleDocumentDepartmentRow(
            document_ref="doc-supplier",
            document_department_ref="dep-supplier",
            document_department_name="Поставщики",
            recommended_folder_ref="folder-suppliers",
            recommended_folder_name="Поставщики",
            document_responsible_ref=None,
            document_responsible_name=None,
            document_author_ref=None,
            document_author_name=None,
        ),
        open_debt_documents=[
            {
                "document_ref": "doc-supplier",
                "document_number": "РТУ-ПОСТАВЩИК",
                "document_date": datetime(2026, 5, 1),
                "open_amount": Decimal("1000.00"),
                "recommended_folder_ref": "folder-suppliers",
                "recommended_folder_name": "Поставщики",
            }
        ],
    )

    assert current_supplier["exclusion_reason"] == "excluded_supplier_folder"
    assert recommended_supplier["exclusion_reason"] == "excluded_supplier_folder"
    guarded = _apply_document_mismatch_guard(
        recommended_supplier,
        diagnostic=OPEN_DEBT_DIAGNOSTIC_TOTAL_ABOVE_BALANCE,
    )
    assert guarded["recommended_folder_ref"] is None
    assert guarded["business_review_reason"] is None
    assert enrich_folder_recommendation_item(guarded)["queue"] == QUEUE_EXCLUDED


def test_canonical_continuous_balance_resolves_maxim_control_documents() -> None:
    cases = [
        {
            "code": "РБ008670",
            "opening_balance": "2940.00",
            "daily_movements": {
                date(2025, 9, 25): Decimal("-5670.00"),
                date(2025, 9, 27): Decimal("-460.00"),
                date(2025, 10, 1): Decimal("7210.00"),
                date(2026, 8, 5): Decimal("-3000.00"),
            },
            "current_balance": "1020.00",
            "expected_number": "РБГУ0477610",
            "expected_date": datetime(2025, 10, 1, 13, 20, 52),
            "gross_amount": "2200.00",
        },
        {
            "code": "РБ028014",
            "opening_balance": "0.00",
            "daily_movements": {
                date(2026, 2, 14): Decimal("9850.00"),
                date(2026, 2, 16): Decimal("-9850.00"),
                date(2026, 3, 8): Decimal("3150.00"),
            },
            "current_balance": "3150.00",
            "expected_number": "РБГУ0106586",
            "expected_date": datetime(2026, 3, 8, 18, 37, 28),
            "gross_amount": "3150.00",
        },
        {
            "code": "РБ008206",
            "opening_balance": "690.00",
            "daily_movements": {
                date(2026, 3, 22): Decimal("-700.00"),
                date(2026, 3, 23): Decimal("1020.00"),
                date(2026, 3, 24): Decimal("-920.00"),
            },
            "current_balance": "90.00",
            "expected_number": "РБГУ0132302",
            "expected_date": datetime(2026, 3, 23, 12, 33, 46),
            "gross_amount": "1020.00",
        },
        {
            "code": "РБ006368",
            "opening_balance": "390.00",
            "daily_movements": {
                date(2026, 4, 25): Decimal("-420.00"),
                date(2026, 5, 3): Decimal("1700.00"),
                date(2026, 5, 4): Decimal("-10.00"),
            },
            "current_balance": "1660.00",
            "expected_number": "РБГУ0198680",
            "expected_date": datetime(2026, 5, 3, 9, 52, 48),
            "gross_amount": "1700.00",
        },
    ]

    for case in cases:
        expected_date = case["expected_date"]
        resolution = resolve_canonical_debt_origin(
            opening_period=date(2025, 1, 1),
            opening_balance=Decimal(case["opening_balance"]),
            daily_movements=case["daily_movements"],
            sale_candidates=[
                CanonicalDebtSaleCandidate(
                    document_ref=f"{case['code']}-expected",
                    document_number=case["expected_number"],
                    document_date=expected_date,
                    gross_amount=Decimal(case["gross_amount"]),
                ),
                CanonicalDebtSaleCandidate(
                    document_ref=f"{case['code']}-newer",
                    document_number="РТУ-НОВЕЕ",
                    document_date=datetime(2026, 7, 1, 12, 0),
                    gross_amount=Decimal("9999.00"),
                ),
            ],
            current_balance=Decimal(case["current_balance"]),
        )

        assert resolution.status == CANONICAL_DEBT_STATUS_MATCHED, case["code"]
        assert [item.document_number for item in resolution.documents] == [case["expected_number"]]
        assert resolution.documents[0].document_date == expected_date
        assert resolution.documents[0].open_amount == Decimal(case["current_balance"])


def test_canonical_continuous_balance_does_not_guess_on_amount_mismatch() -> None:
    resolution = resolve_canonical_debt_origin(
        opening_period=date(2025, 1, 1),
        opening_balance=Decimal("0.00"),
        daily_movements={date(2026, 5, 1): Decimal("1000.00")},
        sale_candidates=[
            CanonicalDebtSaleCandidate(
                document_ref="sale-1",
                document_number="РТУ-1",
                document_date=datetime(2026, 5, 1, 10, 0),
                gross_amount=Decimal("1000.00"),
            )
        ],
        current_balance=Decimal("999.98"),
    )

    assert resolution.status == CANONICAL_DEBT_STATUS_BALANCE_MISMATCH
    assert resolution.documents == ()


def test_multiple_confirmed_open_folders_are_sent_to_business_review() -> None:
    snapshot = _snapshot(
        "cp-multiple-folders",
        counterparty_name="Клиент с двумя папками",
        balance="1000.00",
        document_ref="doc-old",
        document_number="РТУ-СТАРАЯ",
        document_date=datetime(2025, 5, 1),
        credit_depth_days=7,
        is_overdue=True,
        overdue_days=300,
    )
    item = _build_item(
        snapshot,
        folder_row=CounterpartyFolderRow(
            counterparty_ref="cp-multiple-folders",
            counterparty_code="РБ000100",
            counterparty_name="Клиент с двумя папками",
            current_folder_ref="folder-main",
            current_folder_name="01. Горбушка",
        ),
        document_row=SaleDocumentDepartmentRow(
            document_ref="doc-old",
            document_department_ref="dep-a",
            document_department_name="Просвещение",
            recommended_folder_ref="folder-a",
            recommended_folder_name="Просвещение",
            document_responsible_ref="staff-a",
            document_responsible_name="Сотрудник А",
            document_author_ref=None,
            document_author_name=None,
        ),
        open_debt_documents=[
            {
                "document_ref": "doc-old",
                "document_number": "РТУ-СТАРАЯ",
                "document_date": datetime(2025, 5, 1),
                "open_amount": Decimal("400.00"),
                "recommended_folder_ref": "folder-a",
                "recommended_folder_name": "Просвещение",
            },
            {
                "document_ref": "doc-new",
                "document_number": "РТУ-НОВАЯ",
                "document_date": datetime(2026, 5, 1),
                "open_amount": Decimal("600.00"),
                "recommended_folder_ref": "folder-b",
                "recommended_folder_name": "Горбушка",
            },
        ],
    )
    enriched = enrich_folder_recommendation_item(item)

    assert enriched["business_review_reason"] == "multiple_open_debt_folders"
    assert enriched["recommended_folder_ref"] is None
    assert enriched["recommended_folder_name"] is None
    assert enriched["queue"] == QUEUE_BUSINESS_REVIEW
    assert (
        enrich_folder_recommendation_item({**item, "status": STATUS_NO_OVERDUE})["queue"]
        == QUEUE_BUSINESS_REVIEW
    )


def test_folder_alias_treats_site_and_online_store_as_equivalent() -> None:
    item = _build_item(
        _snapshot(
            "cp-alias",
            counterparty_name="Клиент онлайн",
            balance="1200.00",
            document_ref="doc-alias",
            document_number="РТУ-А",
            document_date=datetime(2026, 5, 1, 10, 0),
            credit_depth_days=7,
            is_overdue=True,
            overdue_days=20,
        ),
        folder_row=CounterpartyFolderRow(
            counterparty_ref="cp-alias",
            counterparty_code="РБ000001",
            counterparty_name="Клиент онлайн",
            current_folder_ref="folder-site",
            current_folder_name="08. Сайт",
        ),
        document_row=SaleDocumentDepartmentRow(
            document_ref="doc-alias",
            document_department_ref="dep-online",
            document_department_name="Онлайн-магазин",
            recommended_folder_ref="folder-online",
            recommended_folder_name="Онлайн-магазин",
            document_responsible_ref=None,
            document_responsible_name=None,
            document_author_ref=None,
            document_author_name=None,
        ),
        open_debt_documents=[
            {
                "document_ref": "doc-alias",
                "document_number": "РТУ-А",
                "document_date": datetime(2026, 5, 1, 10, 0),
                "open_amount": Decimal("1200.00"),
                "recommended_folder_name": "Онлайн-магазин",
            }
        ],
    )

    assert item["status"] == STATUS_OK
    assert item["current_folder_display_name"] == "Онлайн-магазин"
    assert item["recommended_folder_display_name"] == "Онлайн-магазин"


def test_folder_alias_treats_teply_stan_and_elektromir_as_equivalent() -> None:
    item = _build_item(
        _snapshot(
            "cp-teply",
            counterparty_name="Клиент Теплый Стан",
            balance="1200.00",
            document_ref="doc-teply",
            document_number="РТУ-Т",
            document_date=datetime(2026, 5, 1, 10, 0),
            credit_depth_days=7,
            is_overdue=True,
            overdue_days=20,
        ),
        folder_row=CounterpartyFolderRow(
            counterparty_ref="cp-teply",
            counterparty_code="РБ000002",
            counterparty_name="Клиент Теплый Стан",
            current_folder_ref="folder-elektromir",
            current_folder_name="МСК-025 Радиорынок Электромир",
        ),
        document_row=SaleDocumentDepartmentRow(
            document_ref="doc-teply",
            document_department_ref="dep-teply",
            document_department_name="04.Теплый Стан",
            recommended_folder_ref="folder-teply",
            recommended_folder_name="04.Теплый Стан",
            document_responsible_ref=None,
            document_responsible_name=None,
            document_author_ref=None,
            document_author_name=None,
        ),
        open_debt_documents=[
            {
                "document_ref": "doc-teply",
                "document_number": "РТУ-Т",
                "document_date": datetime(2026, 5, 1, 10, 0),
                "open_amount": Decimal("1200.00"),
                "recommended_folder_name": "04.Теплый Стан",
            }
        ],
    )

    assert item["status"] == STATUS_OK
    assert item["current_folder_display_name"] == "04.Теплый Стан"
    assert item["recommended_folder_display_name"] == "04.Теплый Стан"


def test_folder_recommendation_prefers_responsible_folder() -> None:
    item = _build_item(
        _snapshot(
            "cp-responsible-folder",
            counterparty_name="Клиент сайта",
            balance="1200.00",
            document_ref="doc-responsible-folder",
            document_number="РТУ-ОТВ",
            document_date=datetime(2026, 5, 1, 10, 0),
            credit_depth_days=7,
            is_overdue=True,
            overdue_days=20,
        ),
        folder_row=CounterpartyFolderRow(
            counterparty_ref="cp-responsible-folder",
            counterparty_code="РБ000019",
            counterparty_name="Клиент сайта",
            current_folder_ref="folder-site",
            current_folder_name="08. Сайт",
        ),
        document_row=SaleDocumentDepartmentRow(
            document_ref="doc-responsible-folder",
            document_department_ref="dept-spb",
            document_department_name="СПБ",
            recommended_folder_ref="folder-spb",
            recommended_folder_name="02. СПБ",
            document_responsible_ref="responsible-site",
            document_responsible_name="Ответственный сайта",
            document_author_ref=None,
            document_author_name=None,
            responsible_department_ref="dept-site",
            responsible_department_name="Сайт",
            responsible_folder_ref="folder-site",
            responsible_folder_name="08. Сайт",
        ),
        open_debt_documents=[
            {
                "document_ref": "doc-responsible-folder",
                "document_number": "РТУ-ОТВ",
                "document_date": datetime(2026, 5, 1, 10, 0),
                "open_amount": Decimal("1200.00"),
                "document_responsible_ref": "responsible-site",
                "document_responsible_name": "Ответственный сайта",
            }
        ],
    )

    assert item["status"] == STATUS_OK
    assert item["debt_department_name"] == "СПБ"
    assert item["debt_document_responsible_name"] == "Ответственный сайта"
    assert item["recommended_folder_name"] == "08. Сайт"
    assert item["recommended_folder_source"] == "responsible_department"


def test_folder_recommendation_ignores_fired_responsible_folder() -> None:
    item = _build_item(
        _snapshot(
            "cp-fired-responsible",
            counterparty_name="Клиент СПБ",
            balance="1200.00",
            document_ref="doc-fired-responsible",
            document_number="РТУ-УВ",
            document_date=datetime(2026, 5, 1, 10, 0),
            credit_depth_days=7,
            is_overdue=True,
            overdue_days=20,
        ),
        folder_row=CounterpartyFolderRow(
            counterparty_ref="cp-fired-responsible",
            counterparty_code="РБ000020",
            counterparty_name="Клиент СПБ",
            current_folder_ref="folder-spb",
            current_folder_name="02. СПБ",
        ),
        document_row=SaleDocumentDepartmentRow(
            document_ref="doc-fired-responsible",
            document_department_ref="dept-spb",
            document_department_name="СПБ",
            recommended_folder_ref="folder-spb",
            recommended_folder_name="02. СПБ",
            document_responsible_ref="responsible-fired",
            document_responsible_name="Уволенный сотрудник",
            document_author_ref=None,
            document_author_name=None,
            responsible_department_ref="person-dept-fired",
            responsible_department_name="Уволенные",
            responsible_folder_ref=None,
            responsible_folder_name="Уволенные",
        ),
        open_debt_documents=[
            {
                "document_ref": "doc-fired-responsible",
                "document_number": "РТУ-УВ",
                "document_date": datetime(2026, 5, 1, 10, 0),
                "open_amount": Decimal("1200.00"),
                "document_responsible_ref": "responsible-fired",
                "document_responsible_name": "Уволенный сотрудник",
            }
        ],
    )

    assert item["status"] == STATUS_OK
    assert item["recommended_folder_name"] == "02. СПБ"
    assert item["recommended_folder_source"] == "document_department"


def _seed_app_db(engine) -> None:
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                _snapshot(
                    "cp-site",
                    counterparty_name="Контрагент из папки Сайт",
                    balance="12000.00",
                    document_ref="doc-old-spb",
                    document_number="РТУ-1",
                    document_date=datetime(2026, 5, 1, 10, 0),
                    credit_depth_days=7,
                    is_overdue=True,
                    overdue_days=21,
                ),
                _snapshot(
                    "cp-ok",
                    counterparty_name="Контрагент СПБ",
                    balance="9000.00",
                    document_ref="doc-spb-ok",
                    document_number="РТУ-2",
                    document_date=datetime(2026, 5, 10, 10, 0),
                    credit_depth_days=7,
                    is_overdue=True,
                    overdue_days=12,
                ),
                _snapshot(
                    "cp-fresh",
                    counterparty_name="Свежий долг Митино",
                    balance="8000.00",
                    document_ref="doc-mitino",
                    document_number="РТУ-3",
                    document_date=datetime(2026, 5, 27, 10, 0),
                    credit_depth_days=7,
                    is_overdue=False,
                    overdue_days=0,
                ),
                _snapshot(
                    "cp-missing-term-mismatch",
                    counterparty_name="Долг без срока оплаты",
                    balance="12900.00",
                    document_ref="doc-missing-term-spb",
                    document_number="РТУ-6",
                    document_date=datetime(2026, 5, 20, 10, 0),
                    credit_depth_days=None,
                    is_overdue=False,
                    overdue_days=None,
                ),
                _snapshot(
                    "cp-below-min",
                    counterparty_name="Мелкий долг ниже порога",
                    balance="499.99",
                    document_ref="doc-below-min-spb",
                    document_number="РТУ-6-1",
                    document_date=datetime(2026, 5, 1, 10, 0),
                    credit_depth_days=7,
                    is_overdue=True,
                    overdue_days=21,
                ),
                _snapshot(
                    "cp-min-threshold",
                    counterparty_name="Долг ровно на пороге",
                    balance="500.00",
                    document_ref="doc-min-threshold-spb",
                    document_number="РТУ-6-2",
                    document_date=datetime(2026, 5, 20, 10, 0),
                    credit_depth_days=7,
                    is_overdue=True,
                    overdue_days=2,
                ),
                _snapshot(
                    "cp-exact-seven-days",
                    counterparty_name="Долг ровно семь дней",
                    balance="501.00",
                    document_ref="doc-exact-seven-spb",
                    document_number="РТУ-6-3",
                    document_date=datetime(2026, 5, 22, 10, 0),
                    credit_depth_days=7,
                    is_overdue=True,
                    overdue_days=0,
                ),
                _snapshot(
                    "cp-missing-term-fresh",
                    counterparty_name="Свежий долг без срока оплаты",
                    balance="4900.00",
                    document_ref="doc-missing-term-fresh",
                    document_number="РТУ-7",
                    document_date=datetime(2026, 5, 27, 10, 0),
                    credit_depth_days=None,
                    is_overdue=False,
                    overdue_days=None,
                ),
                _snapshot(
                    "cp-employee",
                    counterparty_name="Сотрудник тестовый",
                    balance="5100.00",
                    document_ref="doc-employee-spb",
                    document_number="РТУ-8",
                    document_date=datetime(2026, 5, 1, 10, 0),
                    credit_depth_days=7,
                    is_overdue=True,
                    overdue_days=21,
                ),
                _snapshot(
                    "cp-employee-missing-document",
                    counterparty_name="Сотрудник без найденной накладной",
                    balance="5150.00",
                    document_ref="doc-employee-missing",
                    document_number="РТУ-8-1",
                    document_date=datetime(2026, 5, 1, 10, 0),
                    credit_depth_days=7,
                    is_overdue=True,
                    overdue_days=21,
                ),
                _snapshot(
                    "cp-wholesale",
                    counterparty_name="Оптовый клиент Карданов",
                    balance="5200.00",
                    document_ref="doc-wholesale-spb",
                    document_number="РТУ-9",
                    document_date=datetime(2026, 5, 1, 10, 0),
                    credit_depth_days=7,
                    is_overdue=True,
                    overdue_days=21,
                ),
                _snapshot(
                    "cp-pickup-without-payment",
                    counterparty_name="Выдача без оплаты - сайт",
                    balance="5300.00",
                    document_ref="doc-pickup-spb",
                    document_number="РТУ-10",
                    document_date=datetime(2026, 5, 1, 10, 0),
                    credit_depth_days=7,
                    is_overdue=True,
                    overdue_days=21,
                ),
                _snapshot(
                    "cp-spb-cross",
                    counterparty_name="Межпапочный СПБ",
                    balance="5400.00",
                    document_ref="doc-spb-sadovaya",
                    document_number="РТУ-11",
                    document_date=datetime(2026, 5, 1, 10, 0),
                    credit_depth_days=7,
                    is_overdue=True,
                    overdue_days=21,
                ),
                _snapshot(
                    "cp-same-folder-missing-term",
                    counterparty_name="Та же папка без срока оплаты",
                    balance="5500.00",
                    document_ref="doc-same-spb",
                    document_number="РТУ-12",
                    document_date=datetime(2026, 5, 1, 10, 0),
                    credit_depth_days=None,
                    is_overdue=False,
                    overdue_days=None,
                ),
                _snapshot(
                    "cp-review-folder",
                    counterparty_name="Нет папки подразделения",
                    balance="7000.00",
                    document_ref="doc-no-folder",
                    document_number="РТУ-4",
                    document_date=datetime(2026, 5, 2, 10, 0),
                    credit_depth_days=7,
                    is_overdue=True,
                    overdue_days=20,
                ),
                _snapshot(
                    "cp-review-document",
                    counterparty_name="Не найден документ",
                    balance="6000.00",
                    document_ref="doc-missing",
                    document_number="РТУ-5",
                    document_date=datetime(2026, 5, 3, 10, 0),
                    credit_depth_days=7,
                    is_overdue=True,
                    overdue_days=19,
                ),
                _snapshot(
                    "cp-china-supplier",
                    counterparty_name="Поставщик Китай",
                    balance="8000.00",
                    document_ref="doc-china-spb",
                    document_number="РТУ-13",
                    document_date=datetime(2026, 5, 1, 10, 0),
                    credit_depth_days=7,
                    is_overdue=True,
                    overdue_days=21,
                ),
                _snapshot(
                    "cp-old-closed-fresh-open",
                    counterparty_name="Старая закрыта, свежая открыта",
                    balance="1000.00",
                    document_ref="doc-old-closed",
                    document_number="РТУ-14",
                    document_date=datetime(2026, 5, 1, 10, 0),
                    credit_depth_days=7,
                    is_overdue=True,
                    overdue_days=21,
                ),
                _snapshot(
                    "cp-maklab",
                    counterparty_name="Маклаб СПБ ПРОСВЕТ",
                    balance="9000.00",
                    document_ref="doc-maklab-spb",
                    document_number="РТУ-15",
                    document_date=datetime(2026, 5, 1, 10, 0),
                    credit_depth_days=7,
                    is_overdue=True,
                    overdue_days=21,
                ),
            ]
        )
        session.add_all(
            [
                _ledger_sale(
                    "cp-site",
                    document_ref="doc-old-spb",
                    document_number="РТУ-1",
                    document_date=datetime(2026, 5, 1, 10, 0),
                    amount="12000.00",
                ),
                _ledger_sale(
                    "cp-site",
                    document_ref="doc-site-open-a",
                    document_number="РТУ-1-А",
                    document_date=datetime(2026, 5, 2, 10, 0),
                    amount="8000.00",
                ),
                _ledger_sale(
                    "cp-site",
                    document_ref="doc-site-open-b",
                    document_number="РТУ-1-Б",
                    document_date=datetime(2026, 5, 3, 10, 0),
                    amount="4000.00",
                ),
                _ledger_sale(
                    "cp-ok",
                    document_ref="doc-spb-ok",
                    document_number="РТУ-2",
                    document_date=datetime(2026, 5, 10, 10, 0),
                    amount="9000.00",
                ),
                _ledger_sale(
                    "cp-fresh",
                    document_ref="doc-mitino",
                    document_number="РТУ-3",
                    document_date=datetime(2026, 5, 27, 10, 0),
                    amount="8000.00",
                ),
                _ledger_sale(
                    "cp-missing-term-mismatch",
                    document_ref="doc-missing-term-spb",
                    document_number="РТУ-6",
                    document_date=datetime(2026, 5, 20, 10, 0),
                    amount="12900.00",
                ),
                _ledger_sale(
                    "cp-below-min",
                    document_ref="doc-below-min-spb",
                    document_number="РТУ-6-1",
                    document_date=datetime(2026, 5, 1, 10, 0),
                    amount="499.99",
                ),
                _ledger_sale(
                    "cp-min-threshold",
                    document_ref="doc-min-threshold-spb",
                    document_number="РТУ-6-2",
                    document_date=datetime(2026, 5, 20, 10, 0),
                    amount="500.00",
                ),
                _ledger_sale(
                    "cp-min-threshold",
                    document_ref="doc-min-threshold-open-spb",
                    document_number="РТУ-6-2-А",
                    document_date=datetime(2026, 5, 21, 10, 0),
                    amount="500.00",
                ),
                _ledger_sale(
                    "cp-exact-seven-days",
                    document_ref="doc-exact-seven-spb",
                    document_number="РТУ-6-3",
                    document_date=datetime(2026, 5, 22, 10, 0),
                    amount="501.00",
                ),
                _ledger_sale(
                    "cp-missing-term-fresh",
                    document_ref="doc-missing-term-fresh",
                    document_number="РТУ-7",
                    document_date=datetime(2026, 5, 27, 10, 0),
                    amount="4900.00",
                ),
                _ledger_sale(
                    "cp-employee",
                    document_ref="doc-employee-spb",
                    document_number="РТУ-8",
                    document_date=datetime(2026, 5, 1, 10, 0),
                    amount="5100.00",
                ),
                _ledger_sale(
                    "cp-wholesale",
                    document_ref="doc-wholesale-spb",
                    document_number="РТУ-9",
                    document_date=datetime(2026, 5, 1, 10, 0),
                    amount="5200.00",
                ),
                _ledger_sale(
                    "cp-pickup-without-payment",
                    document_ref="doc-pickup-spb",
                    document_number="РТУ-10",
                    document_date=datetime(2026, 5, 1, 10, 0),
                    amount="5300.00",
                ),
                _ledger_sale(
                    "cp-spb-cross",
                    document_ref="doc-spb-sadovaya",
                    document_number="РТУ-11",
                    document_date=datetime(2026, 5, 1, 10, 0),
                    amount="5400.00",
                ),
                _ledger_sale(
                    "cp-same-folder-missing-term",
                    document_ref="doc-same-spb",
                    document_number="РТУ-12",
                    document_date=datetime(2026, 5, 1, 10, 0),
                    amount="5500.00",
                ),
                _ledger_sale(
                    "cp-review-folder",
                    document_ref="doc-no-folder",
                    document_number="РТУ-4",
                    document_date=datetime(2026, 5, 2, 10, 0),
                    amount="7000.00",
                ),
                _ledger_sale(
                    "cp-review-document",
                    document_ref="doc-missing",
                    document_number="РТУ-5",
                    document_date=datetime(2026, 5, 3, 10, 0),
                    amount="6000.00",
                ),
                _ledger_sale(
                    "cp-china-supplier",
                    document_ref="doc-china-spb",
                    document_number="РТУ-13",
                    document_date=datetime(2026, 5, 1, 10, 0),
                    amount="8000.00",
                ),
                _ledger_sale(
                    "cp-old-closed-fresh-open",
                    document_ref="doc-old-closed",
                    document_number="РТУ-14",
                    document_date=datetime(2026, 5, 1, 10, 0),
                    amount="1000.00",
                ),
                ReceivableLedgerEvent(
                    source="test",
                    business_key="cp-old-closed-fresh-open:pko-old-closed",
                    event_type="payment",
                    external_document_ref="pko-old-closed",
                    external_document_number="ПКО-14",
                    external_document_date=datetime(2026, 5, 2, 10, 0),
                    counterparty_ref="cp-old-closed-fresh-open",
                    counterparty_name="cp-old-closed-fresh-open",
                    manager_ref="mgr-origin",
                    manager_name="Менеджер долга",
                    source_layer="regular_receivables",
                    amount_delta=Decimal("-1000.00"),
                ),
                _ledger_sale(
                    "cp-old-closed-fresh-open",
                    document_ref="doc-fresh-open-after-closed",
                    document_number="РТУ-14-А",
                    document_date=datetime(2026, 5, 27, 10, 0),
                    amount="1000.00",
                ),
                _ledger_sale(
                    "cp-maklab",
                    document_ref="doc-maklab-spb",
                    document_number="РТУ-15",
                    document_date=datetime(2026, 5, 1, 10, 0),
                    amount="9000.00",
                ),
            ]
        )
        session.commit()


def _seed_onec_engine():
    engine = _make_sqlite_engine()
    with engine.begin() as conn:
        conn.execute(text("""
                CREATE TABLE _Reference54 (
                    _IDRRef TEXT PRIMARY KEY,
                    _ParentIDRRef TEXT,
                    _Code TEXT,
                    _Description TEXT,
                    _Marked INTEGER NOT NULL DEFAULT 0,
                    _Folder INTEGER NOT NULL DEFAULT 1
                )
                """))
        conn.execute(text("""
                CREATE TABLE _Reference68 (
                    _IDRRef TEXT PRIMARY KEY,
                    _Description TEXT,
                    _Fld8927RRef TEXT
                )
                """))
        conn.execute(text("""
                CREATE TABLE _Document203 (
                    _IDRRef TEXT PRIMARY KEY,
                    _Number TEXT,
                    _Date_Time TEXT,
                    _Fld4937RRef TEXT,
                    _Fld4950RRef TEXT,
                    _Fld4942RRef TEXT,
                    _Fld4939_RTRef TEXT,
                    _Fld4939_RRRef TEXT
                )
                """))
        conn.execute(text("""
                CREATE TABLE _Reference69 (
                    _IDRRef TEXT PRIMARY KEY,
                    _Description TEXT,
                    _Fld9524RRef TEXT,
                    _Fld915RRef TEXT
                )
                """))
        conn.execute(text("""
                CREATE TABLE _Reference94 (
                    _IDRRef TEXT PRIMARY KEY,
                    _ParentIDRRef TEXT,
                    _Description TEXT
                )
                """))
        conn.execute(text("""
                CREATE TABLE _Document132 (
                    _IDRRef TEXT PRIMARY KEY,
                    _Number TEXT,
                    _Date_Time TEXT
                )
                """))
        conn.execute(text("""
                CREATE TABLE _AccumRg7550 (
                    _Active INTEGER,
                    _RecorderTRef TEXT,
                    _RecorderRRef TEXT,
                    _Fld7562 NUMERIC
                )
                """))
        conn.execute(text("""
                CREATE TABLE _Document196 (
                    _IDRRef TEXT PRIMARY KEY,
                    _Number TEXT,
                    _Date_Time TEXT,
                    _Marked INTEGER,
                    _Posted INTEGER,
                    _Fld4688 NUMERIC,
                    _Fld4697_RTRef TEXT,
                    _Fld4697_RRRef TEXT
                )
                """))
        conn.execute(text("""
                CREATE TABLE _Document201 (
                    _IDRRef TEXT PRIMARY KEY,
                    _Number TEXT,
                    _Date_Time TEXT,
                    _Marked INTEGER,
                    _Posted INTEGER,
                    _Fld4852 NUMERIC,
                    _Fld4862_RTRef TEXT,
                    _Fld4862_RRRef TEXT
                )
                """))
        conn.execute(text("""
                INSERT INTO _Reference54 (_IDRRef, _ParentIDRRef, _Code, _Description, _Marked, _Folder)
                VALUES
                    ('folder-site', NULL, NULL, '08. Сайт', 0, 0),
                    ('folder-spb', NULL, NULL, '02. СПБ', 0, 0),
                    ('folder-spb-moscow', NULL, NULL, '13. СПБ Московская', 0, 0),
                    ('folder-spb-sadovaya', NULL, NULL, '09. СПБ Садовая', 0, 0),
                    ('folder-mitino', NULL, NULL, '03. Митино', 0, 0),
                    ('folder-grand', NULL, NULL, '06. Гранд Юг', 0, 0),
                    ('folder-employees', NULL, NULL, '5 .Пятигорск (сотрудники)', 0, 0),
                    ('folder-wholesale', NULL, NULL, '11. Оптовый отдел', 0, 0),
                    ('folder-china-suppliers', NULL, NULL, 'Поставщики Китай', 0, 0),
                    ('author-spb', NULL, NULL, 'Автор СПБ', 0, 0),
                    ('author-site', NULL, NULL, 'Автор сайта', 0, 0),
                    ('author-sadovaya', NULL, NULL, 'Автор Садовая', 0, 0),
                    ('cp-site', 'folder-site', 'РБ053785', 'Контрагент из папки Сайт', 0, 1),
                    ('cp-ok', 'folder-spb', 'РБ000002', 'Контрагент СПБ', 0, 1),
                    ('cp-fresh', 'folder-mitino', 'РБ000003', 'Свежий долг Митино', 0, 1),
                    ('cp-missing-term-mismatch', 'folder-grand', 'РБ000004', 'Долг без срока оплаты', 0, 1),
                    ('cp-below-min', 'folder-grand', 'РБ000005', 'Мелкий долг ниже порога', 0, 1),
                    ('cp-min-threshold', 'folder-grand', 'РБ000006', 'Долг ровно на пороге', 0, 1),
                    ('cp-exact-seven-days', 'folder-grand', 'РБ000007', 'Долг ровно семь дней', 0, 1),
                    ('cp-missing-term-fresh', 'folder-grand', 'РБ000008', 'Свежий долг без срока оплаты', 0, 1),
                    ('cp-employee', 'folder-employees', 'РБ000009', 'Сотрудник тестовый', 0, 1),
                    ('cp-employee-missing-document', 'folder-employees', 'РБ000010', 'Сотрудник без найденной накладной', 0, 1),
                    ('cp-wholesale', 'folder-wholesale', 'РБ000011', 'Оптовый клиент Карданов', 0, 1),
                    ('cp-pickup-without-payment', 'folder-site', 'РБ000012', 'Выдача без оплаты - сайт', 0, 1),
                    ('cp-spb-cross', 'folder-spb-moscow', 'РБ000013', 'Межпапочный СПБ', 0, 1),
                    ('cp-same-folder-missing-term', 'folder-spb', 'РБ000014', 'Та же папка без срока оплаты', 0, 1),
                    ('cp-review-folder', 'folder-site', 'РБ000015', 'Нет папки подразделения', 0, 1),
                    ('cp-review-document', 'folder-site', 'РБ000016', 'Не найден документ', 0, 1),
                    ('cp-china-supplier', 'folder-china-suppliers', 'РБ000017', 'Поставщик Китай', 0, 1),
                    ('cp-old-closed-fresh-open', 'folder-grand', 'РБ000018', 'Старая закрыта, свежая открыта', 0, 1),
                    ('cp-maklab', 'folder-spb-moscow', 'РБ028196', 'Маклаб СПБ ПРОСВЕТ', 0, 1)
                """))
        conn.execute(text("""
                INSERT INTO _Reference69 (_IDRRef, _Description, _Fld9524RRef, _Fld915RRef)
                VALUES
                    ('responsible-spb', 'Ответственный СПБ', NULL, 'person-spb'),
                    ('responsible-site', 'Ответственный сайта', NULL, 'person-site'),
                    ('responsible-mitino', 'Ответственный Митино', NULL, 'person-mitino'),
                    ('responsible-sadovaya', 'Ответственный Садовая', NULL, 'person-sadovaya'),
                    ('responsible-no-folder', 'Ответственный без папки', NULL, NULL)
                """))
        conn.execute(text("""
                INSERT INTO _Reference94 (_IDRRef, _ParentIDRRef, _Description)
                VALUES
                    ('person-dept-spb', NULL, '02. СПБ'),
                    ('person-dept-site', NULL, '08. Сайт'),
                    ('person-dept-mitino', NULL, '03. Митино'),
                    ('person-dept-sadovaya', NULL, '09. СПБ Садовая'),
                    ('person-spb', 'person-dept-spb', 'Ответственный СПБ'),
                    ('person-site', 'person-dept-site', 'Ответственный сайта'),
                    ('person-mitino', 'person-dept-mitino', 'Ответственный Митино'),
                    ('person-sadovaya', 'person-dept-sadovaya', 'Ответственный Садовая')
                """))
        conn.execute(text("""
                INSERT INTO _Reference68 (_IDRRef, _Description, _Fld8927RRef)
                VALUES
                    ('dept-spb', 'СПБ', 'folder-spb'),
                    ('dept-site', 'Сайт', 'folder-site'),
                    ('dept-spb-sadovaya', 'СПБ Садовая', 'folder-spb-sadovaya'),
                    ('dept-mitino', 'Митино', 'folder-mitino'),
                    ('dept-no-folder', 'Подразделение без папки', NULL)
                """))
        conn.execute(text("""
                INSERT INTO _Document132 (_IDRRef, _Number, _Date_Time)
                VALUES
                    ('order-min-threshold', 'ЗКП-1', '2026-05-20 09:50:00')
                """))
        conn.execute(text("""
                INSERT INTO _Document203 (
                    _IDRRef,
                    _Number,
                    _Date_Time,
                    _Fld4937RRef,
                    _Fld4950RRef,
                    _Fld4942RRef,
                    _Fld4939_RTRef,
                    _Fld4939_RRRef
                )
                VALUES
                    ('doc-old-spb', 'РТУ-1', '2026-05-01 10:00:00', 'dept-spb', 'responsible-site', 'author-site', NULL, NULL),
                    ('doc-site-open-a', 'РТУ-1-А', '2026-05-02 10:00:00', 'dept-spb', 'responsible-site', 'author-site', NULL, NULL),
                    ('doc-site-open-b', 'РТУ-1-Б', '2026-05-03 10:00:00', 'dept-spb', 'responsible-site', 'author-site', NULL, NULL),
                    ('doc-spb-ok', 'РТУ-2', '2026-05-10 10:00:00', 'dept-spb', 'responsible-spb', 'author-spb', NULL, NULL),
                    ('doc-mitino', 'РТУ-3', '2026-05-27 10:00:00', 'dept-mitino', 'responsible-mitino', 'author-spb', NULL, NULL),
                    ('doc-missing-term-spb', 'РТУ-6', '2026-05-20 10:00:00', 'dept-spb', 'responsible-spb', 'author-spb', NULL, NULL),
                    ('doc-below-min-spb', 'РТУ-6-1', '2026-05-01 10:00:00', 'dept-spb', 'responsible-spb', 'author-spb', NULL, NULL),
                    ('doc-min-threshold-spb', 'РТУ-6-2', '2026-05-20 10:00:00', 'dept-spb', 'responsible-spb', 'author-spb', '0x00000084', 'order-min-threshold'),
                    ('doc-min-threshold-open-spb', 'РТУ-6-2-А', '2026-05-21 10:00:00', 'dept-spb', 'responsible-spb', 'author-spb', NULL, NULL),
                    ('doc-exact-seven-spb', 'РТУ-6-3', '2026-05-22 10:00:00', 'dept-spb', 'responsible-spb', 'author-spb', NULL, NULL),
                    ('doc-missing-term-fresh', 'РТУ-7', '2026-05-27 10:00:00', 'dept-spb', 'responsible-spb', 'author-spb', NULL, NULL),
                    ('doc-employee-spb', 'РТУ-8', '2026-05-01 10:00:00', 'dept-spb', 'responsible-spb', 'author-spb', NULL, NULL),
                    ('doc-wholesale-spb', 'РТУ-9', '2026-05-01 10:00:00', 'dept-spb', 'responsible-spb', 'author-spb', NULL, NULL),
                    ('doc-pickup-spb', 'РТУ-10', '2026-05-01 10:00:00', 'dept-spb', 'responsible-site', 'author-site', NULL, NULL),
                    ('doc-spb-sadovaya', 'РТУ-11', '2026-05-01 10:00:00', 'dept-spb-sadovaya', 'responsible-sadovaya', 'author-sadovaya', NULL, NULL),
                    ('doc-same-spb', 'РТУ-12', '2026-05-01 10:00:00', 'dept-spb', 'responsible-spb', 'author-spb', NULL, NULL),
                    ('doc-no-folder', 'РТУ-4', '2026-05-02 10:00:00', 'dept-no-folder', 'responsible-no-folder', 'author-spb', NULL, NULL),
                    ('doc-china-spb', 'РТУ-13', '2026-05-01 10:00:00', 'dept-spb', 'responsible-spb', 'author-spb', NULL, NULL),
                    ('doc-old-closed', 'РТУ-14', '2026-05-01 10:00:00', 'dept-spb', 'responsible-spb', 'author-spb', NULL, NULL),
                    ('doc-fresh-open-after-closed', 'РТУ-14-А', '2026-05-27 10:00:00', 'dept-spb', 'responsible-spb', 'author-spb', NULL, NULL),
                    ('doc-maklab-spb', 'РТУ-15', '2026-05-01 10:00:00', 'dept-spb-sadovaya', 'responsible-sadovaya', 'author-sadovaya', NULL, NULL)
                """))
        conn.execute(text("""
                INSERT INTO _AccumRg7550 (_Active, _RecorderTRef, _RecorderRRef, _Fld7562)
                VALUES
                    (1, '0x000000CB', 'doc-old-spb', 12000.00),
                    (1, '0x000000CB', 'doc-site-open-a', 8000.00),
                    (1, '0x000000CB', 'doc-site-open-b', 4000.00),
                    (1, '0x000000CB', 'doc-spb-ok', 9000.00),
                    (1, '0x000000CB', 'doc-mitino', 8000.00),
                    (1, '0x000000CB', 'doc-missing-term-spb', 12900.00),
                    (1, '0x000000CB', 'doc-below-min-spb', 499.99),
                    (1, '0x000000CB', 'doc-min-threshold-spb', 500.00),
                    (1, '0x000000CB', 'doc-min-threshold-open-spb', 500.00),
                    (1, '0x000000CB', 'doc-exact-seven-spb', 501.00),
                    (1, '0x000000CB', 'doc-missing-term-fresh', 4900.00),
                    (1, '0x000000CB', 'doc-employee-spb', 5100.00),
                    (1, '0x000000CB', 'doc-wholesale-spb', 5200.00),
                    (1, '0x000000CB', 'doc-pickup-spb', 5300.00),
                    (1, '0x000000CB', 'doc-spb-sadovaya', 5400.00),
                    (1, '0x000000CB', 'doc-same-spb', 5500.00),
                    (1, '0x000000CB', 'doc-no-folder', 7000.00),
                    (1, '0x000000CB', 'doc-china-spb', 8000.00),
                    (1, '0x000000CB', 'doc-old-closed', 1000.00),
                    (1, '0x000000CB', 'doc-fresh-open-after-closed', 1000.00),
                    (1, '0x000000CB', 'doc-maklab-spb', 9000.00)
                """))
        conn.execute(text("""
                INSERT INTO _Document196 (
                    _IDRRef,
                    _Number,
                    _Date_Time,
                    _Marked,
                    _Posted,
                    _Fld4688,
                    _Fld4697_RTRef,
                    _Fld4697_RRRef
                )
                VALUES
                    (
                        'pko-old-spb',
                        'ПКО-OLD',
                        '2026-05-04 11:00:00',
                        0,
                        1,
                        12000.00,
                        '0x000000CB',
                        'doc-old-spb'
                    ),
                    (
                        'pko-old-closed',
                        'ПКО-14',
                        '2026-05-02 10:00:00',
                        0,
                        1,
                        1000.00,
                        '0x000000CB',
                        'doc-old-closed'
                    ),
                    (
                        'pko-min-threshold',
                        'ПКО-1',
                        '2026-05-21 11:00:00',
                        0,
                        1,
                        500.00,
                        '0x00000084',
                        'order-min-threshold'
                    )
                """))
    return engine


def test_counterparty_folder_recommendations_builds_statuses(tmp_path) -> None:
    app_db_path = tmp_path / "app.db"
    app_engine = _make_sqlite_engine(str(app_db_path))
    onec_engine = _seed_onec_engine()
    _seed_app_db(app_engine)

    with Session(app_engine) as session:
        report = build_counterparty_folder_recommendations(
            session,
            onec_engine=onec_engine,
            snapshot_date=SNAPSHOT_DATE,
        )

    by_ref = {item["counterparty_ref"]: item for item in report["payload"]}
    assert by_ref["cp-site"]["status"] == STATUS_OK
    assert by_ref["cp-site"]["counterparty_code"] == "РБ053785"
    assert by_ref["cp-site"]["current_folder_name"] == "08. Сайт"
    assert by_ref["cp-site"]["recommended_folder_name"] == "08. Сайт"
    assert by_ref["cp-site"]["recommended_folder_source"] == "responsible_department"
    assert by_ref["cp-site"]["debt_department_name"] == "СПБ"
    assert by_ref["cp-site"]["review_reason"] is None
    assert by_ref["cp-site"]["debt_document_number"] == "РТУ-1-А"
    assert by_ref["cp-site"]["debt_document_responsible_name"] == "Ответственный сайта"
    assert by_ref["cp-site"]["debt_document_author_name"] == "Автор сайта"
    assert by_ref["cp-site"]["origin_document_number"] == "РТУ-1"
    assert [doc["document_number"] for doc in by_ref["cp-site"]["open_debt_documents"]] == [
        "РТУ-1-А",
        "РТУ-1-Б",
    ]
    assert by_ref["cp-site"]["open_debt_documents"][0]["statement_selection_rule"] == (
        "statement_structure_confirmed_open"
    )
    assert by_ref["cp-ok"]["status"] == STATUS_OK
    assert by_ref["cp-fresh"]["status"] == STATUS_NO_OVERDUE
    assert by_ref["cp-missing-term-mismatch"]["status"] == STATUS_NEEDS_REVIEW
    assert by_ref["cp-missing-term-mismatch"]["review_reason"] == (
        "origin_document_structure_confirmed_manual_review"
    )
    assert by_ref["cp-missing-term-mismatch"]["document_structure_status"] == "confirmed_open"
    assert by_ref["cp-missing-term-mismatch"]["document_structure_open_amount"] == Decimal(
        "12900.00"
    )
    assert by_ref["cp-missing-term-mismatch"]["effective_credit_depth_days"] == 7
    assert by_ref["cp-missing-term-mismatch"]["effective_payment_term_source"] == (
        "fallback_7_days_read_only"
    )
    assert by_ref["cp-missing-term-mismatch"]["effective_due_date"] == datetime(2026, 5, 27, 10, 0)
    assert by_ref["cp-missing-term-mismatch"]["effective_overdue_days"] == 2
    assert by_ref["cp-below-min"]["status"] == STATUS_NO_OVERDUE
    assert by_ref["cp-below-min"]["review_reason"] == "below_min_balance_threshold"
    assert by_ref["cp-min-threshold"]["status"] == STATUS_NEEDS_REVIEW
    assert by_ref["cp-min-threshold"]["review_reason"] == (
        "origin_document_structure_confirmed_manual_review"
    )
    assert by_ref["cp-min-threshold"]["debt_document_number"] == "РТУ-6-2-А"
    assert by_ref["cp-min-threshold"]["origin_document_number"] == "РТУ-6-2"
    assert by_ref["cp-min-threshold"]["document_structure_status"] == "confirmed_open"
    assert by_ref["cp-min-threshold"]["document_structure_open_amount"] == Decimal("500.00")
    assert by_ref["cp-exact-seven-days"]["status"] == STATUS_NO_OVERDUE
    assert by_ref["cp-exact-seven-days"]["effective_overdue_days"] == 0
    assert by_ref["cp-missing-term-fresh"]["status"] == STATUS_NO_OVERDUE
    assert (
        by_ref["cp-missing-term-fresh"]["review_reason"] == "folder_mismatch_payment_term_missing"
    )
    assert by_ref["cp-employee"]["status"] == STATUS_NO_OVERDUE
    assert by_ref["cp-employee"]["review_reason"] == "excluded_employee_folder"
    assert by_ref["cp-employee-missing-document"]["status"] == STATUS_NO_OVERDUE
    assert by_ref["cp-employee-missing-document"]["review_reason"] == (
        "open_debt_statement_missing"
    )
    assert by_ref["cp-employee-missing-document"]["open_debt_source_status"] == (
        "document_mismatch"
    )
    assert by_ref["cp-employee-missing-document"]["origin_document_number"] is None
    assert by_ref["cp-employee-missing-document"]["effective_due_date"] is None
    assert by_ref["cp-employee-missing-document"]["effective_overdue_days"] is None
    assert by_ref["cp-employee-missing-document"]["is_overdue"] is False
    assert by_ref["cp-wholesale"]["status"] == STATUS_NO_OVERDUE
    assert by_ref["cp-wholesale"]["review_reason"] == "excluded_wholesale_counterparty"
    assert by_ref["cp-pickup-without-payment"]["status"] == STATUS_NO_OVERDUE
    assert by_ref["cp-pickup-without-payment"]["review_reason"] == "excluded_site_payment_on_pickup"
    assert by_ref["cp-china-supplier"]["status"] == STATUS_NO_OVERDUE
    assert by_ref["cp-china-supplier"]["review_reason"] == "excluded_china_supplier_group"
    assert by_ref["cp-old-closed-fresh-open"]["status"] == STATUS_NO_OVERDUE
    assert by_ref["cp-old-closed-fresh-open"]["debt_document_number"] == "РТУ-14-А"
    assert by_ref["cp-old-closed-fresh-open"]["effective_overdue_days"] == 0
    assert by_ref["cp-maklab"]["status"] == STATUS_NO_OVERDUE
    assert by_ref["cp-maklab"]["review_reason"] == "excluded_maklab_spb_prosvet"
    assert by_ref["cp-spb-cross"]["status"] == STATUS_NEEDS_REVIEW
    assert by_ref["cp-spb-cross"]["review_reason"] == "spb_cross_folder_manual_review"
    assert by_ref["cp-spb-cross"]["debt_department_name"] == "СПБ Садовая"
    assert by_ref["cp-same-folder-missing-term"]["status"] == STATUS_OK
    assert by_ref["cp-review-folder"]["status"] == STATUS_NEEDS_REVIEW
    assert by_ref["cp-review-folder"]["review_reason"] == "department_folder_missing"
    assert by_ref["cp-review-document"]["status"] == STATUS_NEEDS_REVIEW
    assert by_ref["cp-review-document"]["review_reason"] == ("origin_document_not_found")
    assert report["summary"]["source_snapshot_count"] == 19
    assert report["summary"]["move_recommended_count"] == 0
    assert report["summary"]["ok_count"] == 3
    assert report["summary"]["no_overdue_count"] == 11
    assert report["summary"]["needs_review_count"] == 5
    assert report["summary"]["below_min_balance_count"] == 1
    assert report["summary"]["document_mismatch_count"] == 1
    assert report["summary"]["min_recommendation_balance"] == Decimal("500.00")
    assert report["summary"]["review_reason_counts"] == {
        "department_folder_missing": 1,
        "origin_document_not_found": 1,
        "origin_document_structure_confirmed_manual_review": 2,
        "spb_cross_folder_manual_review": 1,
    }

    app_engine.dispose()
    onec_engine.dispose()


def test_counterparty_folder_recommendations_can_filter_move_recommended(tmp_path) -> None:
    app_db_path = tmp_path / "app.db"
    app_engine = _make_sqlite_engine(str(app_db_path))
    onec_engine = _seed_onec_engine()
    _seed_app_db(app_engine)

    with Session(app_engine) as session:
        report = build_counterparty_folder_recommendations(
            session,
            onec_engine=onec_engine,
            snapshot_date=SNAPSHOT_DATE,
            status=STATUS_MOVE_RECOMMENDED,
            limit=1,
        )

    assert report["summary"]["source_snapshot_count"] == 19
    assert report["summary"]["total_count"] == 0
    assert report["summary"]["move_recommended_count"] == 0
    assert report["payload"] == []

    app_engine.dispose()
    onec_engine.dispose()


def test_counterparty_folder_recommendations_can_limit_ui_candidates(tmp_path) -> None:
    app_db_path = tmp_path / "app.db"
    app_engine = _make_sqlite_engine(str(app_db_path))
    onec_engine = _seed_onec_engine()
    _seed_app_db(app_engine)

    with Session(app_engine) as session:
        report = build_counterparty_folder_recommendations(
            session,
            onec_engine=onec_engine,
            snapshot_date=SNAPSHOT_DATE,
            candidate_limit=3,
        )

    assert report["summary"]["source_snapshot_count"] == 19
    assert report["summary"]["candidate_snapshot_count"] == 3
    assert len(report["payload"]) == 3

    app_engine.dispose()
    onec_engine.dispose()


def test_counterparty_folder_recommendations_api(monkeypatch, tmp_path) -> None:
    app_db_path = tmp_path / "app.db"
    app_engine = _make_sqlite_engine(str(app_db_path))
    onec_engine = _seed_onec_engine()
    _seed_app_db(app_engine)

    monkeypatch.setenv("MANAGEMENT_INTERNAL_API_TOKEN", "secret-token")
    get_settings.cache_clear()
    app.dependency_overrides = {get_db: _override_db(app_engine)}
    monkeypatch.setattr("app.api.management._build_onec_engine", lambda: onec_engine)
    client = TestClient(app)

    response = client.get(
        "/api/management/counterparty-folder-recommendations",
        params={"date": SNAPSHOT_DATE.isoformat(), "status": STATUS_MOVE_RECOMMENDED},
        headers={"Authorization": "Bearer secret-token"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["as_of"] == SNAPSHOT_DATE.isoformat()
    assert payload["source_status"] == "ready"
    assert payload["summary"]["source_snapshot_count"] == 19
    assert payload["summary"]["move_recommended_count"] == 0
    assert payload["summary"]["below_min_balance_count"] == 1
    assert payload["payload"] == []

    app.dependency_overrides = {}
    get_settings.cache_clear()
    app_engine.dispose()
    onec_engine.dispose()
    if os.path.exists(app_db_path):
        os.remove(app_db_path)


def test_counterparty_folder_snapshot_and_changes_api(monkeypatch, tmp_path) -> None:
    app_db_path = tmp_path / "app.db"
    app_engine = _make_sqlite_engine(str(app_db_path))
    onec_engine = _seed_onec_engine()
    onec_engine_for_changes = _seed_onec_engine()
    _seed_app_db(app_engine)
    previous_date = SNAPSHOT_DATE - timedelta(days=1)

    with Session(app_engine) as session:
        session.add(
            CounterpartyFolderSnapshot(
                snapshot_date=previous_date,
                counterparty_ref="cp-site",
                counterparty_name="Контрагент из папки Сайт",
                current_folder_ref="folder-grand",
                current_folder_name="06. Гранд Юг",
            )
        )
        session.commit()

    monkeypatch.setenv("MANAGEMENT_INTERNAL_API_TOKEN", "secret-token")
    get_settings.cache_clear()
    app.dependency_overrides = {get_db: _override_db(app_engine)}
    onec_engines = iter([onec_engine, onec_engine_for_changes])
    monkeypatch.setattr("app.api.management._build_onec_engine", lambda: next(onec_engines))
    client = TestClient(app)

    sync_response = client.post(
        "/api/management/counterparty-folder-snapshots/sync",
        params={"date": SNAPSHOT_DATE.isoformat()},
        headers={"Authorization": "Bearer secret-token"},
    )
    assert sync_response.status_code == 200
    assert sync_response.json()["summary"]["fetched_count"] == 19

    changes_response = client.get(
        "/api/management/counterparty-folder-changes",
        params={"date": SNAPSHOT_DATE.isoformat()},
        headers={"Authorization": "Bearer secret-token"},
    )
    assert changes_response.status_code == 200
    payload = changes_response.json()
    assert payload["as_of"] == SNAPSHOT_DATE.isoformat()
    assert payload["previous_as_of"] == previous_date.isoformat()
    assert payload["summary"]["total_count"] == 1
    assert payload["summary"]["debt_enrichment_status"] == "ready"
    item = payload["payload"][0]
    assert item["counterparty_ref"] == "cp-site"
    assert item["old_folder_name"] == "06. Гранд Юг"
    assert item["new_folder_name"] == "08. Сайт"
    assert item["current_balance"] == "12000.00"
    assert item["recommended_folder_name"] == "08. Сайт"

    app.dependency_overrides = {}
    get_settings.cache_clear()
    app_engine.dispose()
    onec_engine.dispose()
    onec_engine_for_changes.dispose()
    if os.path.exists(app_db_path):
        os.remove(app_db_path)


def test_counterparty_folder_snapshot_sync_reads_active_counterparties_only(tmp_path) -> None:
    app_db_path = tmp_path / "app.db"
    app_engine = _make_sqlite_engine(str(app_db_path))
    onec_engine = _seed_onec_engine()
    Base.metadata.create_all(app_engine)

    with Session(app_engine) as session:
        result = sync_counterparty_folder_snapshot(
            session,
            onec_engine=onec_engine,
            snapshot_date=SNAPSHOT_DATE,
        )

    assert result.fetched_count == 19
    assert result.inserted_count == 19
    with Session(app_engine) as session:
        rows = session.query(CounterpartyFolderSnapshot).all()
    assert len(rows) == 19
    assert {row.counterparty_ref for row in rows} == {
        "cp-site",
        "cp-ok",
        "cp-fresh",
        "cp-missing-term-mismatch",
        "cp-below-min",
        "cp-min-threshold",
        "cp-exact-seven-days",
        "cp-missing-term-fresh",
        "cp-employee",
        "cp-employee-missing-document",
        "cp-wholesale",
        "cp-pickup-without-payment",
        "cp-spb-cross",
        "cp-same-folder-missing-term",
        "cp-review-folder",
        "cp-review-document",
        "cp-china-supplier",
        "cp-old-closed-fresh-open",
        "cp-maklab",
    }

    app_engine.dispose()
    onec_engine.dispose()


def test_counterparty_folder_changes_first_day_has_no_false_changes(tmp_path) -> None:
    app_db_path = tmp_path / "app.db"
    app_engine = _make_sqlite_engine(str(app_db_path))
    Base.metadata.create_all(app_engine)

    with Session(app_engine) as session:
        session.add(
            CounterpartyFolderSnapshot(
                snapshot_date=SNAPSHOT_DATE,
                counterparty_ref="cp-site",
                counterparty_name="Контрагент из папки Сайт",
                current_folder_ref="folder-site",
                current_folder_name="08. Сайт",
            )
        )
        session.commit()
        report = build_counterparty_folder_changes(session, snapshot_date=SNAPSHOT_DATE)

    assert report["previous_snapshot_date"] is None
    assert report["summary"]["total_count"] == 0
    assert report["payload"] == []

    app_engine.dispose()


def test_counterparty_folder_changes_detects_daily_move_with_debt_context(tmp_path) -> None:
    app_db_path = tmp_path / "app.db"
    app_engine = _make_sqlite_engine(str(app_db_path))
    Base.metadata.create_all(app_engine)
    previous_date = SNAPSHOT_DATE - timedelta(days=1)

    with Session(app_engine) as session:
        session.add_all(
            [
                CounterpartyFolderSnapshot(
                    snapshot_date=previous_date,
                    counterparty_ref="cp-site",
                    counterparty_name="Контрагент из папки Сайт",
                    current_folder_ref="folder-grand",
                    current_folder_name="06. Гранд Юг",
                ),
                CounterpartyFolderSnapshot(
                    snapshot_date=SNAPSHOT_DATE,
                    counterparty_ref="cp-site",
                    counterparty_name="Контрагент из папки Сайт",
                    current_folder_ref="folder-site",
                    current_folder_name="08. Сайт",
                ),
            ]
        )
        session.commit()
        report = build_counterparty_folder_changes(
            session,
            snapshot_date=SNAPSHOT_DATE,
            recommendations_report={
                "payload": [
                    {
                        "counterparty_ref": "cp-site",
                        "current_balance": "12000.00",
                        "origin_document_ref": "doc-old-spb",
                        "origin_document_number": "РТУ-1",
                        "recommended_folder_ref": "folder-spb",
                        "recommended_folder_name": "02. СПБ",
                    }
                ]
            },
        )

    assert report["previous_snapshot_date"] == previous_date
    assert report["summary"]["total_count"] == 1
    assert report["summary"]["open_debt_count"] == 1
    item = report["payload"][0]
    assert item["counterparty_ref"] == "cp-site"
    assert item["old_folder_name"] == "06. Гранд Юг"
    assert item["new_folder_name"] == "08. Сайт"
    assert item["current_balance"] == "12000.00"
    assert item["origin_document_number"] == "РТУ-1"
    assert item["recommended_folder_name"] == "02. СПБ"

    app_engine.dispose()
