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
    STATUS_MOVE_RECOMMENDED,
    STATUS_NEEDS_REVIEW,
    STATUS_NO_OVERDUE,
    STATUS_OK,
    build_counterparty_folder_recommendations,
)
from app.services.counterparty_folder_snapshots import (
    build_counterparty_folder_changes,
    sync_counterparty_folder_snapshot,
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
) -> ReceivableStatementEvent:
    return ReceivableStatementEvent(
        counterparty_ref="cp-statement",
        event_type=event_type,
        document_ref=ref,
        document_number=number,
        document_date=dt,
        amount_delta=Decimal(amount),
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
            _statement_event("payment", "pko-1", "ПКО-1", datetime(2026, 6, 1, 20), "-1000.00"),
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
                    _Fld4942RRef TEXT,
                    _Fld4939_RTRef TEXT,
                    _Fld4939_RRRef TEXT
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
                    ('cp-china-supplier', 'folder-china-suppliers', 'РБ000017', 'Поставщик Китай', 0, 1)
                """))
        conn.execute(text("""
                INSERT INTO _Reference68 (_IDRRef, _Description, _Fld8927RRef)
                VALUES
                    ('dept-spb', 'СПБ', 'folder-spb'),
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
                    _Fld4942RRef,
                    _Fld4939_RTRef,
                    _Fld4939_RRRef
                )
                VALUES
                    ('doc-old-spb', 'РТУ-1', '2026-05-01 10:00:00', 'dept-spb', 'author-site', NULL, NULL),
                    ('doc-site-open-a', 'РТУ-1-А', '2026-05-02 10:00:00', 'dept-spb', 'author-site', NULL, NULL),
                    ('doc-site-open-b', 'РТУ-1-Б', '2026-05-03 10:00:00', 'dept-spb', 'author-site', NULL, NULL),
                    ('doc-spb-ok', 'РТУ-2', '2026-05-10 10:00:00', 'dept-spb', 'author-spb', NULL, NULL),
                    ('doc-mitino', 'РТУ-3', '2026-05-27 10:00:00', 'dept-mitino', 'author-spb', NULL, NULL),
                    ('doc-missing-term-spb', 'РТУ-6', '2026-05-20 10:00:00', 'dept-spb', 'author-spb', NULL, NULL),
                    ('doc-below-min-spb', 'РТУ-6-1', '2026-05-01 10:00:00', 'dept-spb', 'author-spb', NULL, NULL),
                    ('doc-min-threshold-spb', 'РТУ-6-2', '2026-05-20 10:00:00', 'dept-spb', 'author-spb', '0x00000084', 'order-min-threshold'),
                    ('doc-min-threshold-open-spb', 'РТУ-6-2-А', '2026-05-21 10:00:00', 'dept-spb', 'author-spb', NULL, NULL),
                    ('doc-exact-seven-spb', 'РТУ-6-3', '2026-05-22 10:00:00', 'dept-spb', 'author-spb', NULL, NULL),
                    ('doc-missing-term-fresh', 'РТУ-7', '2026-05-27 10:00:00', 'dept-spb', 'author-spb', NULL, NULL),
                    ('doc-employee-spb', 'РТУ-8', '2026-05-01 10:00:00', 'dept-spb', 'author-spb', NULL, NULL),
                    ('doc-wholesale-spb', 'РТУ-9', '2026-05-01 10:00:00', 'dept-spb', 'author-spb', NULL, NULL),
                    ('doc-pickup-spb', 'РТУ-10', '2026-05-01 10:00:00', 'dept-spb', 'author-site', NULL, NULL),
                    ('doc-spb-sadovaya', 'РТУ-11', '2026-05-01 10:00:00', 'dept-spb-sadovaya', 'author-sadovaya', NULL, NULL),
                    ('doc-same-spb', 'РТУ-12', '2026-05-01 10:00:00', 'dept-spb', 'author-spb', NULL, NULL),
                    ('doc-no-folder', 'РТУ-4', '2026-05-02 10:00:00', 'dept-no-folder', 'author-spb', NULL, NULL),
                    ('doc-china-spb', 'РТУ-13', '2026-05-01 10:00:00', 'dept-spb', 'author-spb', NULL, NULL)
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
                    (1, '0x000000CB', 'doc-china-spb', 8000.00)
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
    assert by_ref["cp-site"]["status"] == STATUS_NEEDS_REVIEW
    assert by_ref["cp-site"]["counterparty_code"] == "РБ053785"
    assert by_ref["cp-site"]["current_folder_name"] == "08. Сайт"
    assert by_ref["cp-site"]["recommended_folder_name"] == "02. СПБ"
    assert by_ref["cp-site"]["debt_department_name"] == "СПБ"
    assert (
        by_ref["cp-site"]["review_reason"]
        == "origin_document_needs_order_payment_check"
    )
    assert by_ref["cp-site"]["debt_document_number"] == "РТУ-1-А"
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
    assert by_ref["cp-missing-term-mismatch"]["effective_due_date"] == datetime(
        2026, 5, 27, 10, 0
    )
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
        by_ref["cp-missing-term-fresh"]["review_reason"]
        == "folder_mismatch_payment_term_missing"
    )
    assert by_ref["cp-employee"]["status"] == STATUS_NO_OVERDUE
    assert by_ref["cp-employee"]["review_reason"] == "excluded_employee_folder"
    assert by_ref["cp-employee-missing-document"]["status"] == STATUS_NO_OVERDUE
    assert (
        by_ref["cp-employee-missing-document"]["review_reason"]
        == "excluded_employee_folder"
    )
    assert by_ref["cp-wholesale"]["status"] == STATUS_NO_OVERDUE
    assert by_ref["cp-wholesale"]["review_reason"] == "excluded_wholesale_counterparty"
    assert by_ref["cp-pickup-without-payment"]["status"] == STATUS_NO_OVERDUE
    assert (
        by_ref["cp-pickup-without-payment"]["review_reason"]
        == "excluded_site_payment_on_pickup"
    )
    assert by_ref["cp-china-supplier"]["status"] == STATUS_NO_OVERDUE
    assert by_ref["cp-china-supplier"]["review_reason"] == "excluded_china_supplier_group"
    assert by_ref["cp-spb-cross"]["status"] == STATUS_NEEDS_REVIEW
    assert by_ref["cp-spb-cross"]["review_reason"] == "spb_cross_folder_manual_review"
    assert by_ref["cp-spb-cross"]["debt_department_name"] == "СПБ Садовая"
    assert by_ref["cp-same-folder-missing-term"]["status"] == STATUS_OK
    assert by_ref["cp-review-folder"]["status"] == STATUS_NEEDS_REVIEW
    assert by_ref["cp-review-folder"]["review_reason"] == "department_folder_missing"
    assert by_ref["cp-review-document"]["status"] == STATUS_NEEDS_REVIEW
    assert by_ref["cp-review-document"]["review_reason"] == (
        "origin_document_not_found"
    )
    assert report["summary"]["source_snapshot_count"] == 17
    assert report["summary"]["move_recommended_count"] == 0
    assert report["summary"]["ok_count"] == 2
    assert report["summary"]["no_overdue_count"] == 9
    assert report["summary"]["needs_review_count"] == 6
    assert report["summary"]["below_min_balance_count"] == 1
    assert report["summary"]["min_recommendation_balance"] == Decimal("500.00")
    assert report["summary"]["review_reason_counts"] == {
        "department_folder_missing": 1,
        "origin_document_not_found": 1,
        "origin_document_needs_order_payment_check": 1,
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

    assert report["summary"]["source_snapshot_count"] == 17
    assert report["summary"]["total_count"] == 0
    assert report["summary"]["move_recommended_count"] == 0
    assert report["payload"] == []

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
    assert payload["summary"]["source_snapshot_count"] == 17
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
    assert sync_response.json()["summary"]["fetched_count"] == 17

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
    assert item["recommended_folder_name"] == "02. СПБ"

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

    assert result.fetched_count == 17
    assert result.inserted_count == 17
    with Session(app_engine) as session:
        rows = session.query(CounterpartyFolderSnapshot).all()
    assert len(rows) == 17
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
