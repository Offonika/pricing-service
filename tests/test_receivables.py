from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.models import (
    Base,
    CounterpartyManagerAssignment,
    ReceivableBalanceSnapshot,
    ReceivableCase,
    ReceivableLedgerEvent,
    ReceivableReconciliationSnapshot,
    StaffMember,
)
from app.services.importers.onec_mutual_settlements import (
    _parse_report_period_end,
    load_onec_mutual_settlements_current_balances_file,
)
from app.services.receivables import (
    AuthoritativeReceivableBalanceRow,
    OneCReceivableLedgerExtractor,
    _build_synthetic_receivable_ref,
    _resolve_counterparty_credit_terms,
    build_receivable_balance_snapshots,
    build_receivable_cases,
    build_receivable_opening_import_events,
    build_receivable_reconciliation_snapshots,
    compute_activity_segment,
    compute_aged_bucket,
    fetch_contract_price_type_mapping_from_onec,
    fetch_counterparty_code_mapping_from_onec_group,
    fetch_counterparty_match_keys_from_onec_group,
    fetch_counterparty_phones_from_onec,
    fetch_counterparty_purchase_amounts_from_onec_sales_returns,
    fetch_current_balances_from_onec,
    fetch_employee_counterparty_refs_from_onec,
    fetch_regular_current_balance_overrides_from_onec,
    fetch_staff_members_from_onec,
    load_receivable_current_balance_overrides,
    load_receivable_current_balance_rows,
    sync_receivable_ledger,
)

NORMALIZED_SQL = """
SELECT
    source,
    event_type,
    external_document_ref,
    external_document_number,
    external_document_date,
    counterparty_ref,
    counterparty_name,
    contract_ref,
    contract_name,
    contract_kind_ref,
    contract_kind_name,
    manager_ref,
    manager_name,
    store_ref,
    store_name,
    source_layer,
    planned_payment_date,
    credit_depth_days,
    shipment_ban,
    line_no,
    amount_delta
FROM onec_receivable_source
ORDER BY external_document_date, line_no
"""

OPENING_BALANCE_SQL = """
SELECT
    'onec' AS source,
    'opening_balance' AS event_type,
    'opening-cp-a' AS external_document_ref,
    'Остаток на дату' AS external_document_number,
    :opening_balance_date AS external_document_date,
    'cp-a' AS counterparty_ref,
    'Контрагент A' AS counterparty_name,
    'contract-a' AS contract_ref,
    'Основной договор' AS contract_name,
    'kind-buyer' AS contract_kind_ref,
    'С покупателем' AS contract_kind_name,
    NULL AS manager_ref,
    NULL AS manager_name,
    NULL AS store_ref,
    NULL AS store_name,
    'employee_summary' AS source_layer,
    NULL AS planned_payment_date,
    NULL AS credit_depth_days,
    0 AS shipment_ban,
    1 AS line_no,
    150 AS amount_delta,
    0 AS skip_ingest
WHERE :opening_balance_date IS NOT NULL
"""

OPENING_BALANCE_SKIP_SQL = """
SELECT
    'onec' AS source,
    'opening_balance' AS event_type,
    'opening-skip' AS external_document_ref,
    'Остаток на дату' AS external_document_number,
    :opening_balance_date AS external_document_date,
    'cp-a' AS counterparty_ref,
    'Контрагент A' AS counterparty_name,
    'contract-a' AS contract_ref,
    'Основной договор' AS contract_name,
    'kind-buyer' AS contract_kind_ref,
    'С покупателем' AS contract_kind_name,
    NULL AS manager_ref,
    NULL AS manager_name,
    NULL AS store_ref,
    NULL AS store_name,
    'employee_summary' AS source_layer,
    NULL AS planned_payment_date,
    NULL AS credit_depth_days,
    0 AS shipment_ban,
    1 AS line_no,
    150 AS amount_delta,
    1 AS skip_ingest
WHERE :opening_balance_date IS NOT NULL
UNION ALL
SELECT
    'onec' AS source,
    'opening_balance' AS event_type,
    'opening-keep' AS external_document_ref,
    'Остаток на дату' AS external_document_number,
    :opening_balance_date AS external_document_date,
    'cp-a' AS counterparty_ref,
    'Контрагент A' AS counterparty_name,
    'contract-a' AS contract_ref,
    'Основной договор' AS contract_name,
    'kind-buyer' AS contract_kind_ref,
    'С покупателем' AS contract_kind_name,
    NULL AS manager_ref,
    NULL AS manager_name,
    NULL AS store_ref,
    NULL AS store_name,
    'employee_summary' AS source_layer,
    NULL AS planned_payment_date,
    NULL AS credit_depth_days,
    0 AS shipment_ban,
    2 AS line_no,
    50 AS amount_delta,
    0 AS skip_ingest
WHERE :opening_balance_date IS NOT NULL
"""


def _setup_onec_source(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("""
                CREATE TABLE onec_receivable_source (
                    source TEXT,
                    event_type TEXT,
                    external_document_ref TEXT,
                    external_document_number TEXT,
                    external_document_date DATETIME,
                    counterparty_ref TEXT,
                    counterparty_name TEXT,
                    contract_ref TEXT,
                    contract_name TEXT,
                    contract_kind_ref TEXT,
                    contract_kind_name TEXT,
                    manager_ref TEXT,
                    manager_name TEXT,
                    store_ref TEXT,
                    store_name TEXT,
                    source_layer TEXT,
                    planned_payment_date DATETIME,
                    credit_depth_days INTEGER,
                    shipment_ban INTEGER,
                    line_no INTEGER,
                    amount_delta NUMERIC
                )
                """))
        conn.execute(text("""
                INSERT INTO onec_receivable_source (
                    source, event_type, external_document_ref, external_document_number,
                    external_document_date, counterparty_ref, counterparty_name,
                    contract_ref, contract_name, contract_kind_ref, contract_kind_name,
                    manager_ref, manager_name, store_ref, store_name,
                    source_layer,
                    planned_payment_date, credit_depth_days, shipment_ban,
                    line_no, amount_delta
                ) VALUES
                    ('onec', 'sale', 'sale-a1', 'S-001', '2026-03-01 10:00:00', 'cp-a', 'Контрагент A', 'contract-a', 'Основной договор A', 'kind-buyer', 'С покупателем', 'mgr-1', 'Менеджер 1', 'store-1', 'Магазин 1', 'regular_receivables', '2026-03-25 00:00:00', 7, 0, 1, 100),
                    ('onec', 'payment', 'pay-a1', 'P-001', '2026-03-05 09:00:00', 'cp-a', 'Контрагент A', 'contract-a', 'Основной договор A', 'kind-buyer', 'С покупателем', 'mgr-1', 'Менеджер 1', 'store-1', 'Магазин 1', 'regular_receivables', '2026-03-25 00:00:00', 7, 0, 1, -30),
                    ('onec', 'sale', 'sale-a2', 'S-002', '2026-03-10 12:00:00', 'cp-a', 'Контрагент A', 'contract-a', 'Основной договор A', 'kind-buyer', 'С покупателем', 'mgr-2', 'Менеджер 2', 'store-1', 'Магазин 1', 'regular_receivables', '2026-03-25 00:00:00', 7, 0, 1, 20),
                    ('onec', 'return', 'return-a1', 'R-001', '2026-03-15 14:00:00', 'cp-a', 'Контрагент A', 'contract-a', 'Основной договор A', 'kind-buyer', 'С покупателем', 'mgr-2', 'Менеджер 2', 'store-1', 'Магазин 1', 'regular_receivables', '2026-03-25 00:00:00', 7, 0, 1, -10),
                    ('onec', 'sale', 'sale-b1', 'S-101', '2026-01-31 10:00:00', 'cp-b', 'Контрагент B', 'contract-b', 'Основной договор B', 'kind-employee', 'С покупателем', 'mgr-3', 'Менеджер 3', 'store-2', 'Магазин 2', 'employee_summary', '2026-02-10 00:00:00', NULL, 0, 1, 40),
                    ('onec', 'sale', 'sale-c1', 'S-201', '2025-12-01 10:00:00', 'cp-c', 'Контрагент C', 'contract-c', 'Основной договор C', 'kind-buyer', 'С покупателем', 'mgr-4', 'Менеджер 4', 'store-3', 'Магазин 3', 'regular_receivables', NULL, 30, 1, 1, 50)
                    ,
                    ('onec', 'sale', 'sale-d1', 'S-301', '2026-03-16 09:30:00', 'cp-d', 'Контрагент D', 'contract-d', 'Основной договор D', 'kind-buyer', 'С покупателем', 'mgr-5', 'Менеджер 5', 'store-4', 'Магазин 4', 'regular_receivables', '2026-03-21 00:00:00', NULL, 0, 1, 70)
                """))


def _setup_onec_opening_mapping(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("""
                CREATE TABLE _Reference54 (
                    _IDRRef TEXT,
                    _Code TEXT,
                    _Description TEXT,
                    _Fld9516 TEXT,
                    _Fld9865 INTEGER,
                    _Fld9866 INTEGER
                )
                """))
        conn.execute(text("""
                CREATE TABLE _Reference37 (
                    _IDRRef TEXT,
                    _OwnerIDRRef TEXT,
                    _Code TEXT,
                    _Description TEXT,
                    _Fld515RRef TEXT
                )
                """))
        conn.execute(text("""
                INSERT INTO _Reference54 (_IDRRef, _Code, _Description, _Fld9516, _Fld9865, _Fld9866)
                VALUES ('cp-ref-1', 'РБ025491', '002 Эксперт', '2025-01-15 00:00:00', 14, 1)
                """))
        conn.execute(text("""
                INSERT INTO _Reference37 (_IDRRef, _OwnerIDRRef, _Code, _Description, _Fld515RRef)
                VALUES ('contract-ref-1', 'cp-ref-1', 'РБ0040473', 'Возврат брака', '0x9363c6f0a10557bf4822a55db4862286')
                """))
        conn.execute(text("""
                INSERT INTO _Reference37 (_IDRRef, _Code, _Description, _Fld515RRef, _OwnerIDRRef)
                VALUES ('contract-ref-2', 'РБ0049999', 'Основной договор', '0x9363c6f0a10557bf4822a55db4862286', 'cp-ref-1')
                """))


def _setup_onec_counterparty_phones(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("""
                CREATE TABLE _Reference25 (
                    _IDRRef TEXT,
                    _Description TEXT
                )
                """))
        conn.execute(text("""
                CREATE TABLE _InfoRg6402 (
                    _Fld6403_RTRef TEXT,
                    _Fld6403_RRRef TEXT,
                    _Fld6405_RRRef TEXT,
                    _Fld6406 TEXT
                )
                """))
        conn.execute(text("""
                INSERT INTO _Reference25 (_IDRRef, _Description) VALUES
                    ('kind-main', 'Телефон контрагента'),
                    ('kind-work', 'Рабочий'),
                    ('kind-extra', 'Доп. телефон для переноса'),
                    ('kind-email', 'Email')
                """))
        conn.execute(text("""
                INSERT INTO _InfoRg6402 (
                    _Fld6403_RTRef, _Fld6403_RRRef, _Fld6405_RRRef, _Fld6406
                ) VALUES
                    ('0x00000036', 'cp-a', 'kind-work', '8 (999) 000-00-01'),
                    ('0x00000036', 'cp-a', 'kind-main', '+7 999 000-00-02'),
                    ('0x00000036', 'cp-b', 'kind-extra', '9990000003'),
                    ('0x00000036', 'cp-c', 'kind-email', 'client@example.test'),
                    ('0x00000037', 'cp-d', 'kind-main', '+7 999 000-00-04')
                """))


def _setup_onec_regular_current_totals(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("""
                CREATE TABLE _Reference54 (
                    _IDRRef TEXT,
                    _Description TEXT
                )
                """))
        conn.execute(text("""
                CREATE TABLE _Reference37 (
                    _IDRRef TEXT,
                    _Fld515RRef TEXT
                )
                """))
        conn.execute(text("""
                CREATE TABLE _AccumRgTn7571 (
                    _Period DATETIME,
                    _Fld7559RRef TEXT,
                    _Fld7562 NUMERIC
                )
                """))
        conn.execute(text("""
                CREATE TABLE _AccumRgT7622 (
                    _Period DATETIME,
                    _Fld7615RRef TEXT,
                    _Fld7619RRef TEXT,
                    _Fld7620 NUMERIC
                )
                """))
        conn.execute(text("""
                INSERT INTO _Reference54 (_IDRRef, _Description) VALUES
                    ('cp-regular', 'ООО \"АйТех Сервис\"'),
                    ('cp-summary', 'ИП ЕВТУШЕНКО-КУДИНА ЮЛИЯ НИКОЛАЕВНА'),
                    ('cp-employee', 'Сотрудник Тестовый')
                """))
        conn.execute(text("""
                INSERT INTO _Reference37 (_IDRRef, _Fld515RRef) VALUES
                    ('contract-buyer', '0x9363c6f0a10557bf4822a55db4862286')
                """))
        conn.execute(text("""
                INSERT INTO _AccumRgTn7571 (_Period, _Fld7559RRef, _Fld7562) VALUES
                    ('2026-03-01 00:00:00', 'cp-regular', 148471.22),
                    ('2026-03-01 00:00:00', 'cp-employee', 5000.00),
                    ('2026-02-01 00:00:00', 'cp-regular', 999.00)
                """))
        conn.execute(text("""
                INSERT INTO _AccumRgT7622 (_Period, _Fld7615RRef, _Fld7619RRef, _Fld7620) VALUES
                    ('2026-03-01 00:00:00', 'contract-buyer', 'cp-summary', 23069.20),
                    ('2026-03-01 00:00:00', 'contract-buyer', 'cp-employee', 9999.00),
                    ('2026-02-01 00:00:00', 'contract-buyer', 'cp-summary', 777.00)
                """))


def test_compute_bucket_and_activity_segments() -> None:
    assert compute_aged_bucket(date(2026, 3, 18), date(2026, 3, 20)) == "0-7"
    assert compute_aged_bucket(date(2026, 2, 15), date(2026, 3, 20)) == "31-60"
    assert compute_aged_bucket(date(2025, 12, 1), date(2026, 3, 20)) == "90+"

    assert compute_activity_segment(datetime(2026, 3, 10, 10, 0, 0), date(2026, 3, 20)) == "active"
    assert (
        compute_activity_segment(datetime(2026, 1, 31, 10, 0, 0), date(2026, 3, 20)) == "low_active"
    )
    assert (
        compute_activity_segment(datetime(2025, 12, 1, 10, 0, 0), date(2026, 3, 20)) == "inactive"
    )


def test_credit_terms_ignore_zero_depth_and_dates_before_origin() -> None:
    origin_event = SimpleNamespace(external_document_date=datetime(2026, 3, 10, 9, 0, 0))
    events = [
        SimpleNamespace(
            planned_payment_date=datetime(1999, 7, 13, 0, 0, 0),
            credit_depth_days=0,
            shipment_ban=False,
            external_document_date=datetime(2026, 3, 10, 9, 0, 0),
        )
    ]

    result = _resolve_counterparty_credit_terms(
        events,
        origin_event=origin_event,
        snapshot_date=date(2026, 3, 20),
    )

    assert result["planned_payment_date"] is None
    assert result["credit_depth_days"] is None
    assert result["payment_term_source"] == "missing"
    assert result["due_date"] is None
    assert result["overdue_days"] is None
    assert result["is_overdue"] is False


def test_fetch_employee_counterparty_refs_from_onec_sorts_and_deduplicates() -> None:
    class FakeResult:
        def mappings(self):
            return self

        def all(self):
            return [
                {"counterparty_ref": "cp-b"},
                {"counterparty_ref": "cp-a"},
                {"counterparty_ref": None},
                {"counterparty_ref": "cp-b"},
            ]

    class FakeConnection:
        def execute(self, _stmt):
            return FakeResult()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

    class FakeEngine:
        def connect(self):
            return FakeConnection()

    assert fetch_employee_counterparty_refs_from_onec(FakeEngine()) == ("cp-a", "cp-b")


def test_build_receivable_opening_import_events_maps_codes_to_refs(tmp_path) -> None:
    onec_engine = create_engine("sqlite:///:memory:")
    _setup_onec_opening_mapping(onec_engine)

    opening_file = tmp_path / "opening.csv"
    opening_file.write_text(
        "\n".join(
            [
                "snapshot_date,currency_name,contract_name,counterparty_code,contract_kind_name,contract_code,settlement_document,opening_balance,opening_balance_rub,source_row",
                "2025-01-01,RMB,Возврат брака,РБ025491,С покупателем,РБ0040473,,348800.46,5412358.02,73",
            ]
        ),
        encoding="utf-8",
    )

    events = build_receivable_opening_import_events(onec_engine, report_path=opening_file)

    assert len(events) == 1
    event = events[0]
    assert event.source == "onec_opening_import"
    assert event.event_type == "opening_balance"
    assert event.counterparty_ref == "cp-ref-1"
    assert event.counterparty_name == "002 Эксперт"
    assert event.contract_ref == "contract-ref-1"
    assert event.contract_name == "Возврат брака"
    assert event.contract_kind_name == "С покупателем"
    assert event.source_layer == "opening_import_1c"
    assert event.line_no == 73
    assert event.amount_delta == Decimal("5412358.02")
    assert event.planned_payment_date == datetime(2025, 1, 15, 0, 0, 0)
    assert event.credit_depth_days == 14
    assert event.shipment_ban is True


def test_build_receivable_opening_import_events_falls_back_to_contract_owner(tmp_path) -> None:
    onec_engine = create_engine("sqlite:///:memory:")
    _setup_onec_opening_mapping(onec_engine)

    opening_file = tmp_path / "opening.csv"
    opening_file.write_text(
        "\n".join(
            [
                "snapshot_date,currency_name,contract_name,counterparty_code,contract_kind_name,contract_code,settlement_document,opening_balance,opening_balance_rub,source_row",
                "2025-01-01,руб,Основной договор,209,С покупателем,РБ0049999,,-30,-30,23284",
            ]
        ),
        encoding="utf-8",
    )

    events = build_receivable_opening_import_events(onec_engine, report_path=opening_file)

    assert len(events) == 1
    event = events[0]
    assert event.counterparty_ref == "cp-ref-1"
    assert event.counterparty_name == "002 Эксперт"
    assert event.contract_ref == "contract-ref-2"
    assert event.contract_name == "Основной договор"
    assert event.amount_delta == Decimal("-30.00")


def test_build_receivable_opening_import_events_uses_synthetic_contract_ref_when_missing(
    tmp_path,
) -> None:
    onec_engine = create_engine("sqlite:///:memory:")
    _setup_onec_opening_mapping(onec_engine)

    opening_file = tmp_path / "opening.csv"
    opening_file.write_text(
        "\n".join(
            [
                "snapshot_date,currency_name,contract_name,counterparty_code,contract_kind_name,contract_code,settlement_document,opening_balance,opening_balance_rub,source_row",
                "2025-01-01,руб,Исторический договор,РБ025491,С покупателем,РБ9999999,,100,100,77",
            ]
        ),
        encoding="utf-8",
    )

    events = build_receivable_opening_import_events(onec_engine, report_path=opening_file)

    assert len(events) == 1
    event = events[0]
    assert event.counterparty_ref == "cp-ref-1"
    assert event.counterparty_name == "002 Эксперт"
    assert event.contract_ref.startswith("synthetic:")
    assert len(event.contract_ref) <= 64
    assert event.contract_name == "Исторический договор"
    assert event.contract_kind_name == "С покупателем"
    assert event.amount_delta == Decimal("100.00")


def test_load_receivable_current_balance_overrides_reads_csv(tmp_path) -> None:
    current_file = tmp_path / "current.csv"
    current_file.write_text(
        "\n".join(
            [
                "snapshot_date,counterparty_name,current_balance_rub,source_row",
                "2026-03-26,Букренев Сергей Леонидович,9710,15",
                "2026-03-26,Зацепин Станислав Сергеевич,-22,16",
            ]
        ),
        encoding="utf-8",
    )

    snapshot_date, overrides = load_receivable_current_balance_overrides(current_file)

    assert snapshot_date == date(2026, 3, 26)
    assert overrides["букренев сергей леонидович"] == Decimal("9710.00")
    assert overrides["зацепин станислав сергеевич"] == Decimal("-22.00")


def test_load_receivable_current_balance_overrides_sums_duplicate_names(tmp_path) -> None:
    current_file = tmp_path / "current_duplicates.csv"
    current_file.write_text(
        "\n".join(
            [
                "snapshot_date,counterparty_name,current_balance_rub,source_row",
                "2026-03-26,Сергей,10,15",
                "2026-03-26,сергей,20,16",
                "2026-03-26,СЕРГЕЙ,30,17",
            ]
        ),
        encoding="utf-8",
    )

    snapshot_date, overrides = load_receivable_current_balance_overrides(current_file)

    assert snapshot_date == date(2026, 3, 26)
    assert overrides["сергей"] == Decimal("60.00")


def test_fetch_regular_current_balance_overrides_from_onec_reads_month_end_totals() -> None:
    onec_engine = create_engine("sqlite:///:memory:")
    _setup_onec_regular_current_totals(onec_engine)

    overrides = fetch_regular_current_balance_overrides_from_onec(
        onec_engine,
        snapshot_date=date(2026, 2, 28),
        employee_counterparty_refs=("cp-employee",),
    )

    assert overrides == {
        'ооо "айтех сервис"': Decimal("148471.22"),
        "ип евтушенко-кудина юлия николаевна": Decimal("23069.20"),
    }


def test_fetch_regular_current_balance_overrides_from_onec_skips_non_month_end() -> None:
    onec_engine = create_engine("sqlite:///:memory:")
    _setup_onec_regular_current_totals(onec_engine)

    overrides = fetch_regular_current_balance_overrides_from_onec(
        onec_engine,
        snapshot_date=date(2026, 2, 27),
        employee_counterparty_refs=("cp-employee",),
    )

    assert overrides == {}


def test_load_receivable_current_balance_rows_assigns_refs_and_sums_duplicates(tmp_path) -> None:
    current_file = tmp_path / "current.csv"
    current_file.write_text(
        "\n".join(
            [
                "snapshot_date,counterparty_name,current_balance_rub,source_row",
                "2026-03-20,Контрагент D,50.00,1",
                "2026-03-20,контрагент d,20.00,2",
            ]
        ),
        encoding="utf-8",
    )

    snapshot_date, rows = load_receivable_current_balance_rows(
        current_file,
        counterparty_mapping={
            "контрагент d": {
                "counterparty_ref": "cp-d",
                "counterparty_name": "Контрагент D",
            }
        },
    )

    assert snapshot_date == date(2026, 3, 20)
    assert rows == [
        AuthoritativeReceivableBalanceRow(
            counterparty_ref="cp-d",
            counterparty_name="Контрагент D",
            current_balance=Decimal("70.00"),
            current_manager_ref=None,
            current_manager_name=None,
            source="onec_current_import",
        )
    ]


def test_load_receivable_current_balance_rows_keeps_unmatched_rows_with_synthetic_ref(
    tmp_path,
) -> None:
    current_file = tmp_path / "current.csv"
    current_file.write_text(
        "\n".join(
            [
                "snapshot_date,counterparty_name,current_balance_rub,source_row",
                "2026-03-20,Контрагент D,70.00,1",
                "2026-03-20,Неизвестный контрагент,-15.00,2",
            ]
        ),
        encoding="utf-8",
    )

    snapshot_date, rows = load_receivable_current_balance_rows(
        current_file,
        counterparty_mapping={
            "контрагент d": {
                "counterparty_ref": "cp-d",
                "counterparty_name": "Контрагент D",
            }
        },
        synthetic_ref_prefix="buyer-current-balance",
    )

    assert snapshot_date == date(2026, 3, 20)
    assert [(row.counterparty_ref, row.counterparty_name, row.current_balance) for row in rows] == [
        ("cp-d", "Контрагент D", Decimal("70.00")),
        (
            _build_synthetic_receivable_ref(
                "buyer-current-balance",
                "неизвестный контрагент",
            ),
            "Неизвестный контрагент",
            Decimal("-15.00"),
        ),
    ]


def test_fetch_current_balances_from_onec_rejects_excel_imports(
    tmp_path,
) -> None:
    onec_engine = create_engine("sqlite:///:memory:")
    buyer_current = tmp_path / "buyers-current.csv"
    buyer_current.write_text(
        "\n".join(
            [
                "snapshot_date,counterparty_name,current_balance_rub,source_row",
                "2026-03-20,Контрагент D,70.00,1",
            ]
        ),
        encoding="utf-8",
    )
    employee_current = tmp_path / "employee-current.csv"
    employee_current.write_text(
        "\n".join(
            [
                "snapshot_date,counterparty_name,current_balance_rub,source_row",
                "2026-03-20,Контрагент B,33.00,1",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Excel current-import больше не поддерживается"):
        fetch_current_balances_from_onec(
            onec_engine,
            snapshot_date=date(2026, 3, 20),
            current_import_path=str(buyer_current),
            employee_current_import_path=str(employee_current),
        )


def test_parse_report_period_end_supports_month_range() -> None:
    assert _parse_report_period_end("Период: Январь 2025 г. - Февраль 2026 г.") == date(2026, 2, 28)


def test_load_onec_mutual_settlements_current_balances_file_reads_xls(tmp_path) -> None:
    source = Path(__file__).resolve().parents[1] / "docs" / "ВедомостьСотрудникипо280226тест.xls"
    target = tmp_path / "staff_current.xls"
    target.write_bytes(source.read_bytes())

    rows = load_onec_mutual_settlements_current_balances_file(target)

    by_name = {row.counterparty_name: row.current_balance_rub for row in rows}
    assert rows[0].snapshot_date == date(2026, 2, 28)
    assert by_name["Букренев Сергей Леонидович"] == Decimal("5878.00")
    assert by_name["Зацепин Станислав Сергеевич"] == Decimal("30.00")
    assert by_name["Бочаров Омар"] == Decimal("18271.85")


def test_fetch_staff_members_from_onec_cleans_names_and_keeps_fired_status() -> None:
    class FakeResult:
        def mappings(self):
            return self

        def all(self):
            return [
                {
                    "external_ref": "phys-2",
                    "full_name": "* Иванов Иван Иванович (сотрудник)",
                    "department_ref": "dep-2",
                    "department_name": "Продажи",
                    "counterparty_ref": "cp-2",
                    "counterparty_name": "Иванов Иван Иванович",
                    "employment_status": "active",
                    "termination_date": None,
                },
                {
                    "external_ref": "phys-1",
                    "full_name": "- Петров Петр Петрович",
                    "department_ref": "dep-1",
                    "department_name": "Уволенные",
                    "counterparty_ref": "cp-1",
                    "counterparty_name": "Петров Петр Петрович",
                    "employment_status": "fired",
                    "termination_date": date(2026, 3, 1),
                },
            ]

    class FakeConnection:
        def execute(self, _stmt):
            return FakeResult()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

    class FakeEngine:
        def connect(self):
            return FakeConnection()

    result = fetch_staff_members_from_onec(FakeEngine())

    assert result == (
        {
            "source": "onec_physical_person",
            "external_ref": "phys-2",
            "full_name": "Иванов Иван Иванович",
            "role_code": None,
            "role_name": None,
            "department_ref": "dep-2",
            "department_name": "Продажи",
            "store_ref": "cp-2",
            "store_name": "Иванов Иван Иванович",
            "employment_status": "active",
            "hire_date": None,
            "termination_date": None,
            "manager_ref": None,
            "manager_name": None,
        },
        {
            "source": "onec_physical_person",
            "external_ref": "phys-1",
            "full_name": "Петров Петр Петрович",
            "role_code": None,
            "role_name": None,
            "department_ref": "dep-1",
            "department_name": "Уволенные",
            "store_ref": "cp-1",
            "store_name": "Петров Петр Петрович",
            "employment_status": "fired",
            "hire_date": None,
            "termination_date": date(2026, 3, 1),
            "manager_ref": None,
            "manager_name": None,
        },
    )


def test_fetch_counterparty_match_keys_from_onec_group_normalizes_values() -> None:
    class FakeResult:
        def mappings(self):
            return self

        def __iter__(self):
            yield {"counterparty_name": ' ООО "АйТех Сервис" '}
            yield {"counterparty_name": "ИП ЕВТУШЕНКО-КУДИНА ЮЛИЯ НИКОЛАЕВНА"}
            yield {"counterparty_name": None}

    class FakeConnection:
        def execute(self, _stmt, params):
            assert params["group_name"] == "ПОКУПАТЕЛИ"
            return FakeResult()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

    class FakeEngine:
        def connect(self):
            return FakeConnection()

    keys = fetch_counterparty_match_keys_from_onec_group(
        FakeEngine(),
        group_name="ПОКУПАТЕЛИ",
    )

    assert keys == {
        'ооо "айтех сервис"',
        "ип евтушенко-кудина юлия николаевна",
    }


def test_fetch_counterparty_code_mapping_from_onec_group_uses_refs() -> None:
    class FakeResult:
        def mappings(self):
            return self

        def __iter__(self):
            yield {
                "counterparty_ref": "0xabc",
                "counterparty_code": " РБ000001 ",
                "counterparty_name": "Клиент 1",
            }
            yield {
                "counterparty_ref": "0xdef",
                "counterparty_code": "",
                "counterparty_name": "Клиент 2",
            }

    class FakeConnection:
        def execute(self, _stmt, params):
            assert params["group_name"] == "ПОКУПАТЕЛИ"
            return FakeResult()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

    class FakeEngine:
        def connect(self):
            return FakeConnection()

    mapping = fetch_counterparty_code_mapping_from_onec_group(
        FakeEngine(),
        group_name="ПОКУПАТЕЛИ",
    )

    assert mapping == {"0XABC": "РБ000001"}


def test_fetch_counterparty_phones_from_onec_prefers_primary_kind() -> None:
    engine = create_engine("sqlite:///:memory:")
    _setup_onec_counterparty_phones(engine)

    try:
        phones = fetch_counterparty_phones_from_onec(
            engine,
            counterparty_refs=["cp-a", "cp-b", "cp-c", "cp-d"],
        )
    finally:
        engine.dispose()

    assert phones == {
        "cp-a": "+79990000002",
        "cp-b": "+79990000003",
    }


def test_fetch_contract_price_type_mapping_from_onec_uses_contract_requisite() -> None:
    class FakeResult:
        def mappings(self):
            return self

        def __iter__(self):
            yield {
                "contract_ref": "contract-a",
                "price_type_name": " 2.Бронзовый ",
            }
            yield {
                "contract_ref": "contract-b",
                "price_type_name": None,
            }

    class FakeConnection:
        def execute(self, stmt, params):
            sql = str(stmt)
            assert "_Fld513_RRRef" in sql
            assert params == {"contract_ref_0": "contract-a"}
            return FakeResult()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

    class FakeDialect:
        name = "sqlite"

    class FakeEngine:
        dialect = FakeDialect()

        def connect(self):
            return FakeConnection()

    mapping = fetch_contract_price_type_mapping_from_onec(
        FakeEngine(),
        contract_refs=["contract-a"],
    )

    assert mapping == {"CONTRACT-A": "2.Бронзовый"}


def test_fetch_counterparty_purchase_amounts_from_onec_sales_returns() -> None:
    class FakeResult:
        def mappings(self):
            return self

        def __iter__(self):
            yield {"counterparty_ref": "0xabc", "purchase_amount": Decimal("123.456")}
            yield {"counterparty_ref": None, "purchase_amount": Decimal("10.00")}

    class FakeConnection:
        def execute(self, stmt, params):
            sql = str(stmt)
            assert "_AccumRg7550" in sql
            assert "contract._Fld515RRef" in sql
            assert params["period_start"] == datetime(2026, 2, 1)
            assert params["period_end"] == datetime(2026, 3, 1)
            return FakeResult()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

    class FakeDialect:
        name = "sqlite"

    class FakeEngine:
        dialect = FakeDialect()

        def connect(self):
            return FakeConnection()

    mapping = fetch_counterparty_purchase_amounts_from_onec_sales_returns(
        FakeEngine(),
        period_start=datetime(2026, 2, 1),
        period_end=datetime(2026, 3, 1),
    )

    assert mapping == {"0XABC": Decimal("123.46")}


def test_extractor_supports_opening_balance_date_param() -> None:
    onec_engine = create_engine("sqlite:///:memory:")
    extractor = OneCReceivableLedgerExtractor(onec_engine, operations_sql=OPENING_BALANCE_SQL)

    events = extractor.fetch_receivable_events(opening_balance_date=date(2025, 1, 1))

    assert len(events) == 1
    assert events[0].event_type == "opening_balance"
    assert events[0].external_document_ref == "opening-cp-a"
    assert events[0].external_document_date == datetime(2025, 1, 1, 0, 0, 0)
    assert events[0].amount_delta == Decimal("150.00")


def test_extractor_skips_rows_marked_with_skip_ingest() -> None:
    onec_engine = create_engine("sqlite:///:memory:")
    extractor = OneCReceivableLedgerExtractor(onec_engine, operations_sql=OPENING_BALANCE_SKIP_SQL)

    events = extractor.fetch_receivable_events(opening_balance_date=date(2025, 1, 1))

    assert len(events) == 1
    assert events[0].external_document_ref == "opening-keep"
    assert events[0].amount_delta == Decimal("50.00")


def test_opening_balance_does_not_create_new_daily_origin_case() -> None:
    onec_engine = create_engine("sqlite:///:memory:")
    app_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(app_engine)

    extractor = OneCReceivableLedgerExtractor(onec_engine, operations_sql=OPENING_BALANCE_SQL)
    events = extractor.fetch_receivable_events(opening_balance_date=date(2025, 1, 1))

    with Session(app_engine) as session:
        sync_receivable_ledger(
            session,
            events,
            snapshot_date=date(2025, 1, 1),
            employee_counterparty_refs=("cp-a",),
        )
        session.commit()

        snapshot = session.execute(select(ReceivableBalanceSnapshot)).scalar_one()
        assert snapshot.current_balance == Decimal("150.00")
        assert snapshot.origin_document_ref is None
        assert snapshot.origin_document_date is None
        assert snapshot.aged_bucket == "unknown"

        segments = {
            item.segment for item in session.execute(select(ReceivableCase)).scalars().all()
        }
        assert "new_daily" not in segments
        assert "employee" in segments


def test_receivable_sync_is_idempotent_and_builds_manager_history() -> None:
    app_engine = create_engine("sqlite:///:memory:")
    onec_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(app_engine)
    _setup_onec_source(onec_engine)

    extractor = OneCReceivableLedgerExtractor(onec_engine, operations_sql=NORMALIZED_SQL)
    events = extractor.fetch_receivable_events()

    with Session(app_engine) as session:
        result1 = sync_receivable_ledger(session, events, snapshot_date=date(2026, 3, 20))
        session.commit()
        assert result1["inserted"] == 7
        assert result1["updated"] == 0
        assert result1["assignments"] == 5
        assert result1["snapshots"] == 4
        assert result1["cases"] == 9

        result2 = sync_receivable_ledger(session, events, snapshot_date=date(2026, 3, 20))
        session.commit()
        assert result2["inserted"] == 0
        assert result2["updated"] == 0
        assert session.query(ReceivableLedgerEvent).count() == 7
        assert session.query(CounterpartyManagerAssignment).count() == 5
        assert session.query(ReceivableCase).count() == 9

        assignments = (
            session.query(CounterpartyManagerAssignment)
            .filter(CounterpartyManagerAssignment.counterparty_ref == "cp-a")
            .order_by(CounterpartyManagerAssignment.effective_from)
            .all()
        )
        assert [item.manager_ref for item in assignments] == ["mgr-1", "mgr-2"]
        assert assignments[0].effective_to == datetime(2026, 3, 10, 12, 0, 0)
        assert assignments[1].effective_to is None


def test_receivable_sync_accepts_streaming_events() -> None:
    app_engine = create_engine("sqlite:///:memory:")
    onec_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(app_engine)
    _setup_onec_source(onec_engine)

    extractor = OneCReceivableLedgerExtractor(onec_engine, operations_sql=NORMALIZED_SQL)
    events = extractor.fetch_receivable_events()

    with Session(app_engine) as session:
        result = sync_receivable_ledger(
            session,
            (event for event in events),
            snapshot_date=date(2026, 3, 20),
            ingest_batch_size=2,
        )
        session.commit()

        assert result["processed"] == 7
        assert result["inserted"] == 7
        assert result["assignments"] == 5
        assert result["snapshots"] == 4
        assert result["cases"] == 9
        assert session.query(ReceivableLedgerEvent).count() == 7
        assert session.query(ReceivableBalanceSnapshot).count() == 4
        assert session.query(ReceivableCase).count() == 9


def test_receivable_sync_updates_existing_credit_terms() -> None:
    app_engine = create_engine("sqlite:///:memory:")
    onec_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(app_engine)
    _setup_onec_source(onec_engine)

    extractor = OneCReceivableLedgerExtractor(onec_engine, operations_sql=NORMALIZED_SQL)

    with Session(app_engine) as session:
        first_events = extractor.fetch_receivable_events()
        first_result = sync_receivable_ledger(
            session, first_events, snapshot_date=date(2026, 3, 20)
        )
        session.commit()
        assert first_result["inserted"] == 7
        assert first_result["updated"] == 0

    with onec_engine.begin() as conn:
        conn.execute(text("""
                UPDATE onec_receivable_source
                SET planned_payment_date = '2026-02-20 00:00:00',
                    shipment_ban = 1
                WHERE counterparty_ref = 'cp-b'
                """))

    with Session(app_engine) as session:
        second_events = extractor.fetch_receivable_events()
        second_result = sync_receivable_ledger(
            session, second_events, snapshot_date=date(2026, 3, 20)
        )
        session.commit()

        assert second_result["inserted"] == 0
        assert second_result["updated"] == 1

        updated_event = (
            session.query(ReceivableLedgerEvent)
            .filter(ReceivableLedgerEvent.external_document_ref == "sale-b1")
            .one()
        )
        assert updated_event.planned_payment_date == datetime(2026, 2, 20, 0, 0, 0)
        assert updated_event.shipment_ban is True


def test_receivable_sync_can_replace_existing_ledger() -> None:
    app_engine = create_engine("sqlite:///:memory:")
    onec_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(app_engine)
    _setup_onec_source(onec_engine)

    extractor = OneCReceivableLedgerExtractor(onec_engine, operations_sql=NORMALIZED_SQL)
    events = extractor.fetch_receivable_events()
    target_event = next(event for event in events if event.external_document_ref == "sale-a1")

    with Session(app_engine) as session:
        first_result = sync_receivable_ledger(
            session,
            events[:3],
            snapshot_date=date(2026, 3, 20),
        )
        session.commit()

        assert first_result["processed"] == 3
        assert session.query(ReceivableLedgerEvent).count() == 3

    with Session(app_engine) as session:
        second_result = sync_receivable_ledger(
            session,
            [target_event],
            snapshot_date=date(2026, 3, 20),
            replace_existing=True,
        )
        session.commit()

        remaining = session.execute(select(ReceivableLedgerEvent)).scalars().all()
        assignments = session.execute(select(CounterpartyManagerAssignment)).scalars().all()
        snapshots = session.execute(select(ReceivableBalanceSnapshot)).scalars().all()
        cases = session.execute(select(ReceivableCase)).scalars().all()

        assert second_result["reset"]["ledger_events_deleted"] == 3
        assert len(remaining) == 1
        assert remaining[0].business_key == target_event.business_key
        assert len(assignments) == 1
        assert len(snapshots) == 1
        assert len(cases) == second_result["cases"]


def test_build_receivable_snapshots_keeps_origin_balance_and_segments() -> None:
    app_engine = create_engine("sqlite:///:memory:")
    onec_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(app_engine)
    _setup_onec_source(onec_engine)

    extractor = OneCReceivableLedgerExtractor(onec_engine, operations_sql=NORMALIZED_SQL)
    events = extractor.fetch_receivable_events()

    with Session(app_engine) as session:
        sync_receivable_ledger(session, events)
        build_receivable_balance_snapshots(session, snapshot_date=date(2026, 3, 20))
        session.commit()

        snapshots = (
            session.query(ReceivableBalanceSnapshot)
            .order_by(ReceivableBalanceSnapshot.counterparty_ref)
            .all()
        )
        assert [item.counterparty_ref for item in snapshots] == ["cp-a", "cp-b", "cp-c", "cp-d"]

        snap_a = next(item for item in snapshots if item.counterparty_ref == "cp-a")
        assert snap_a.current_balance == Decimal("80.00")
        assert snap_a.origin_document_ref == "sale-a1"
        assert snap_a.current_manager_ref == "mgr-2"
        assert snap_a.aged_bucket == "8-30"
        assert snap_a.activity_segment == "active"
        assert snap_a.planned_payment_date == datetime(2026, 3, 25, 0, 0, 0)
        assert snap_a.credit_depth_days == 7
        assert snap_a.due_date == datetime(2026, 3, 25, 0, 0, 0)
        assert snap_a.is_overdue is False
        assert snap_a.payment_term_source == "planned_payment_date"

        snap_b = next(item for item in snapshots if item.counterparty_ref == "cp-b")
        assert snap_b.current_balance == Decimal("40.00")
        assert snap_b.aged_bucket == "31-60"
        assert snap_b.activity_segment == "low_active"
        assert snap_b.due_date == datetime(2026, 2, 10, 0, 0, 0)
        assert snap_b.overdue_days == 38
        assert snap_b.is_overdue is True

        snap_c = next(item for item in snapshots if item.counterparty_ref == "cp-c")
        assert snap_c.current_balance == Decimal("50.00")
        assert snap_c.aged_bucket == "90+"
        assert snap_c.activity_segment == "inactive"
        assert snap_c.credit_depth_days == 30
        assert snap_c.shipment_ban is True
        assert snap_c.due_date == datetime(2025, 12, 31, 10, 0, 0)
        assert snap_c.overdue_days == 79
        assert snap_c.is_overdue is True
        assert snap_c.payment_term_source == "credit_depth_days"


def test_build_receivable_snapshots_skips_regular_rows_duplicated_by_employee_summary() -> None:
    app_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(app_engine)

    duplicated_at = datetime(2026, 3, 20, 18, 34, 42)
    unique_regular_at = datetime(2026, 3, 21, 9, 0, 0)

    with Session(app_engine) as session:
        session.add_all(
            [
                ReceivableLedgerEvent(
                    source="onec",
                    business_key="emp-dup-1",
                    event_type="debt_adjustment",
                    external_document_ref="emp-dup-1",
                    external_document_number=None,
                    external_document_date=duplicated_at,
                    counterparty_ref="cp-employee",
                    counterparty_name="Сотрудник Дубль",
                    contract_ref="contract-employee",
                    contract_name="5.Платиновый",
                    contract_kind_ref="kind-buyer",
                    contract_kind_name="С покупателем",
                    manager_ref=None,
                    manager_name=None,
                    store_ref=None,
                    store_name=None,
                    source_layer="employee_summary",
                    planned_payment_date=None,
                    credit_depth_days=None,
                    shipment_ban=None,
                    line_no=None,
                    amount_delta=Decimal("235.60"),
                ),
                ReceivableLedgerEvent(
                    source="onec",
                    business_key="reg-dup-1",
                    event_type="sale",
                    external_document_ref="reg-dup-1",
                    external_document_number="РБГУ0128241",
                    external_document_date=duplicated_at,
                    counterparty_ref="cp-employee",
                    counterparty_name="Сотрудник Дубль",
                    contract_ref=None,
                    contract_name=None,
                    contract_kind_ref=None,
                    contract_kind_name=None,
                    manager_ref=None,
                    manager_name=None,
                    store_ref=None,
                    store_name=None,
                    source_layer="regular_receivables",
                    planned_payment_date=None,
                    credit_depth_days=None,
                    shipment_ban=None,
                    line_no=None,
                    amount_delta=Decimal("235.60"),
                ),
                ReceivableLedgerEvent(
                    source="onec",
                    business_key="reg-uniq-1",
                    event_type="sale",
                    external_document_ref="reg-uniq-1",
                    external_document_number="РБГУ0129000",
                    external_document_date=unique_regular_at,
                    counterparty_ref="cp-employee",
                    counterparty_name="Сотрудник Дубль",
                    contract_ref=None,
                    contract_name=None,
                    contract_kind_ref=None,
                    contract_kind_name=None,
                    manager_ref=None,
                    manager_name=None,
                    store_ref=None,
                    store_name=None,
                    source_layer="regular_receivables",
                    planned_payment_date=None,
                    credit_depth_days=None,
                    shipment_ban=None,
                    line_no=None,
                    amount_delta=Decimal("100.00"),
                ),
            ]
        )

        build_receivable_balance_snapshots(session, snapshot_date=date(2026, 3, 21))
        session.commit()

        snapshot = (
            session.query(ReceivableBalanceSnapshot)
            .filter(ReceivableBalanceSnapshot.counterparty_ref == "cp-employee")
            .one()
        )

        assert snapshot.current_balance == Decimal("335.60")


def test_build_receivable_snapshots_applies_current_balance_override() -> None:
    app_engine = create_engine("sqlite:///:memory:")
    onec_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(app_engine)
    _setup_onec_source(onec_engine)

    extractor = OneCReceivableLedgerExtractor(onec_engine, operations_sql=NORMALIZED_SQL)
    events = extractor.fetch_receivable_events()

    with Session(app_engine) as session:
        sync_receivable_ledger(session, events)
        result = build_receivable_balance_snapshots(
            session,
            snapshot_date=date(2026, 3, 20),
            current_balance_overrides={"контрагент b": Decimal("33.00")},
        )
        session.commit()

        snapshot = (
            session.query(ReceivableBalanceSnapshot)
            .filter(ReceivableBalanceSnapshot.counterparty_ref == "cp-b")
            .one()
        )

        assert result["overrides_applied"] == 1
        assert snapshot.current_balance == Decimal("33.00")


def test_build_receivable_snapshots_uses_counterparty_group_as_department() -> None:
    app_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(app_engine)

    with Session(app_engine) as session:
        session.add(
            ReceivableLedgerEvent(
                source="onec",
                business_key="sale-cp-pilot",
                event_type="sale",
                external_document_ref="sale-cp-pilot",
                external_document_number="S-001",
                external_document_date=datetime(2026, 3, 10, 10, 0, 0),
                counterparty_ref="cp-pilot",
                counterparty_name="Пилотный покупатель",
                contract_ref="contract-1",
                contract_name="Основной договор",
                contract_kind_ref="kind-buyer",
                contract_kind_name="С покупателем",
                manager_ref="mgr-1",
                manager_name="Менеджер",
                store_ref="store-master-mobile",
                store_name="MASTER MOBILE",
                source_layer="regular_receivables",
                amount_delta=Decimal("100.00"),
            )
        )

        build_receivable_balance_snapshots(
            session,
            snapshot_date=date(2026, 3, 20),
            counterparty_departments_by_ref={
                "cp-pilot": {
                    "department_ref": "dep-gorbushkin",
                    "department_name": "01. Горбушкин Двор",
                }
            },
        )
        session.commit()

        snapshot = session.query(ReceivableBalanceSnapshot).one()
        assert snapshot.department_ref == "dep-gorbushkin"
        assert snapshot.department_name == "01. Горбушкин Двор"


def test_build_receivable_snapshots_uses_oldest_unpaid_sale_as_origin() -> None:
    app_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(app_engine)

    with Session(app_engine) as session:
        session.add_all(
            [
                ReceivableLedgerEvent(
                    source="onec",
                    business_key="sale-old-paid",
                    event_type="sale",
                    external_document_ref="sale-old-paid",
                    external_document_number="S-OLD",
                    external_document_date=datetime(2026, 4, 20, 10, 0, 0),
                    counterparty_ref="cp-fifo",
                    counterparty_name="Покупатель FIFO",
                    amount_delta=Decimal("100.00"),
                ),
                ReceivableLedgerEvent(
                    source="onec",
                    business_key="sale-unpaid-origin",
                    event_type="sale",
                    external_document_ref="sale-unpaid-origin",
                    external_document_number="S-UNPAID",
                    external_document_date=datetime(2026, 5, 3, 12, 0, 0),
                    counterparty_ref="cp-fifo",
                    counterparty_name="Покупатель FIFO",
                    amount_delta=Decimal("40.00"),
                ),
                ReceivableLedgerEvent(
                    source="onec",
                    business_key="payment-closes-old",
                    event_type="payment",
                    external_document_ref="payment-closes-old",
                    external_document_number="P-001",
                    external_document_date=datetime(2026, 5, 4, 9, 0, 0),
                    counterparty_ref="cp-fifo",
                    counterparty_name="Покупатель FIFO",
                    amount_delta=Decimal("-100.00"),
                ),
                ReceivableLedgerEvent(
                    source="onec",
                    business_key="sale-new-tail",
                    event_type="sale",
                    external_document_ref="sale-new-tail",
                    external_document_number="S-TAIL",
                    external_document_date=datetime(2026, 5, 5, 12, 0, 0),
                    counterparty_ref="cp-fifo",
                    counterparty_name="Покупатель FIFO",
                    amount_delta=Decimal("10.00"),
                ),
            ]
        )

        build_receivable_balance_snapshots(session, snapshot_date=date(2026, 5, 8))
        build_receivable_cases(session, snapshot_date=date(2026, 5, 8))
        session.commit()

        snapshot = session.query(ReceivableBalanceSnapshot).one()
        assert snapshot.current_balance == Decimal("50.00")
        assert snapshot.origin_document_number == "S-UNPAID"

        case = session.query(ReceivableCase).filter(ReceivableCase.segment == "buyers").one()
        assert [item["document_number"] for item in case.chain_documents] == [
            "S-UNPAID",
            "S-TAIL",
        ]


def test_build_receivable_snapshots_keeps_negative_override_in_main_snapshot() -> None:
    app_engine = create_engine("sqlite:///:memory:")
    onec_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(app_engine)
    _setup_onec_source(onec_engine)

    extractor = OneCReceivableLedgerExtractor(onec_engine, operations_sql=NORMALIZED_SQL)
    events = extractor.fetch_receivable_events()

    with Session(app_engine) as session:
        sync_receivable_ledger(session, events)
        result = build_receivable_balance_snapshots(
            session,
            snapshot_date=date(2026, 3, 20),
            current_balance_overrides={"контрагент c": Decimal("-25.00")},
        )
        session.commit()

        snapshot = (
            session.query(ReceivableBalanceSnapshot)
            .filter(ReceivableBalanceSnapshot.counterparty_ref == "cp-c")
            .one()
        )

        assert result["overrides_applied"] == 1
        assert snapshot.current_balance == Decimal("-25.00")


def test_build_receivable_snapshots_use_authoritative_rows_without_enrichment() -> None:
    app_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(app_engine)

    with Session(app_engine) as session:
        result = build_receivable_balance_snapshots(
            session,
            snapshot_date=date(2026, 3, 20),
            authoritative_balance_rows=[
                AuthoritativeReceivableBalanceRow(
                    counterparty_ref="cp-auth",
                    counterparty_name="Авторитетный контрагент",
                    current_balance=Decimal("123.45"),
                    source="test_authoritative",
                )
            ],
        )
        session.commit()

        snapshot = (
            session.query(ReceivableBalanceSnapshot)
            .filter(ReceivableBalanceSnapshot.counterparty_ref == "cp-auth")
            .one()
        )

        assert result["snapshots"] == 1
        assert snapshot.current_balance == Decimal("123.45")
        assert snapshot.origin_document_ref is None
        assert snapshot.current_manager_ref is None
        assert snapshot.aged_bucket == "unknown"


def test_build_receivable_snapshots_authoritative_rows_ignore_polluted_ledger_balance() -> None:
    app_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(app_engine)

    with Session(app_engine) as session:
        session.add_all(
            [
                ReceivableLedgerEvent(
                    source="onec_hybrid_opening",
                    business_key="polluted-sale-1",
                    event_type="sale",
                    external_document_ref="polluted-sale-1",
                    external_document_number="РБГУ0001",
                    external_document_date=datetime(2026, 3, 10, 10, 0, 0),
                    counterparty_ref="cp-polluted",
                    counterparty_name="Зашумленный контрагент",
                    contract_ref="contract-1",
                    contract_name="Основной договор",
                    contract_kind_ref="kind-buyer",
                    contract_kind_name="С покупателем",
                    manager_ref="mgr-1",
                    manager_name="Менеджер 1",
                    store_ref=None,
                    store_name=None,
                    source_layer="regular_receivables",
                    amount_delta=Decimal("100.00"),
                ),
                ReceivableLedgerEvent(
                    source="onec_hybrid_opening",
                    business_key="polluted-sale-2",
                    event_type="sale",
                    external_document_ref="polluted-sale-1-duplicate",
                    external_document_number="РБГУ0001",
                    external_document_date=datetime(2026, 3, 10, 10, 0, 0),
                    counterparty_ref="cp-polluted",
                    counterparty_name="Зашумленный контрагент",
                    contract_ref="contract-1",
                    contract_name="Основной договор",
                    contract_kind_ref="kind-buyer",
                    contract_kind_name="С покупателем",
                    manager_ref="mgr-1",
                    manager_name="Менеджер 1",
                    store_ref=None,
                    store_name=None,
                    source_layer="regular_receivables",
                    amount_delta=Decimal("100.00"),
                ),
            ]
        )

        result = build_receivable_balance_snapshots(
            session,
            snapshot_date=date(2026, 3, 20),
            authoritative_balance_rows=[
                AuthoritativeReceivableBalanceRow(
                    counterparty_ref="cp-polluted",
                    counterparty_name="Зашумленный контрагент",
                    current_balance=Decimal("40.00"),
                    source="test_authoritative",
                )
            ],
        )
        session.commit()

        snapshot = (
            session.query(ReceivableBalanceSnapshot)
            .filter(ReceivableBalanceSnapshot.counterparty_ref == "cp-polluted")
            .one()
        )

        assert result["snapshots"] == 1
        assert snapshot.current_balance == Decimal("40.00")
        assert snapshot.origin_document_ref == "polluted-sale-1"
        assert snapshot.current_manager_ref == "mgr-1"


def test_build_receivable_snapshots_strict_overrides_do_not_duplicate_same_name() -> None:
    app_engine = create_engine("sqlite:///:memory:")
    onec_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(app_engine)
    _setup_onec_source(onec_engine)

    extractor = OneCReceivableLedgerExtractor(onec_engine, operations_sql=NORMALIZED_SQL)
    events = extractor.fetch_receivable_events()
    duplicate_name_event = ReceivableLedgerEvent(
        source="onec",
        business_key="dup-name-cp-b",
        event_type="sale",
        external_document_ref="dup-name-cp-b",
        external_document_number="ДУБЛЬ",
        external_document_date=datetime(2026, 3, 20, 12, 0, 0),
        counterparty_ref="cp-b-dup",
        counterparty_name="Контрагент B",
        contract_ref=None,
        contract_name=None,
        contract_kind_ref=None,
        contract_kind_name=None,
        manager_ref=None,
        manager_name=None,
        store_ref=None,
        store_name=None,
        source_layer="regular_receivables",
        planned_payment_date=None,
        credit_depth_days=None,
        shipment_ban=None,
        line_no=999,
        amount_delta=Decimal("10.00"),
    )

    with Session(app_engine) as session:
        sync_receivable_ledger(session, events + [duplicate_name_event])
        result = build_receivable_balance_snapshots(
            session,
            snapshot_date=date(2026, 3, 20),
            current_balance_overrides={"контрагент b": Decimal("33.00")},
            strict_current_balance_overrides=True,
        )
        session.commit()

        snapshots = session.query(ReceivableBalanceSnapshot).all()
        total = sum((snapshot.current_balance for snapshot in snapshots), Decimal("0.00"))

        assert result["snapshots"] == 1
        assert result["overrides_applied"] == 1
        assert len(snapshots) == 1
        assert total == Decimal("33.00")


def test_build_receivable_reconciliation_snapshots_keeps_negative_override() -> None:
    app_engine = create_engine("sqlite:///:memory:")
    onec_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(app_engine)
    _setup_onec_source(onec_engine)

    extractor = OneCReceivableLedgerExtractor(onec_engine, operations_sql=NORMALIZED_SQL)
    events = extractor.fetch_receivable_events()

    with Session(app_engine) as session:
        sync_receivable_ledger(session, events)
        result = build_receivable_reconciliation_snapshots(
            session,
            snapshot_date=date(2026, 3, 20),
            current_balance_overrides={"контрагент c": Decimal("-25.00")},
        )
        session.commit()

        snapshot = (
            session.query(ReceivableReconciliationSnapshot)
            .filter(ReceivableReconciliationSnapshot.counterparty_ref == "cp-c")
            .one()
        )

        assert result["reconciliation_snapshots"] == 4
        assert result["overrides_applied"] == 1
        assert snapshot.signed_balance == Decimal("-25.00")
        assert snapshot.absolute_balance == Decimal("25.00")


def test_build_receivable_reconciliation_strict_overrides_do_not_duplicate_same_name() -> None:
    app_engine = create_engine("sqlite:///:memory:")
    onec_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(app_engine)
    _setup_onec_source(onec_engine)

    extractor = OneCReceivableLedgerExtractor(onec_engine, operations_sql=NORMALIZED_SQL)
    events = extractor.fetch_receivable_events()
    duplicate_name_event = ReceivableLedgerEvent(
        source="onec",
        business_key="dup-name-cp-b-recon",
        event_type="sale",
        external_document_ref="dup-name-cp-b-recon",
        external_document_number="ДУБЛЬ",
        external_document_date=datetime(2026, 3, 20, 12, 0, 0),
        counterparty_ref="cp-b-dup",
        counterparty_name="Контрагент B",
        contract_ref=None,
        contract_name=None,
        contract_kind_ref=None,
        contract_kind_name=None,
        manager_ref=None,
        manager_name=None,
        store_ref=None,
        store_name=None,
        source_layer="regular_receivables",
        planned_payment_date=None,
        credit_depth_days=None,
        shipment_ban=None,
        line_no=998,
        amount_delta=Decimal("10.00"),
    )

    with Session(app_engine) as session:
        sync_receivable_ledger(session, events + [duplicate_name_event])
        result = build_receivable_reconciliation_snapshots(
            session,
            snapshot_date=date(2026, 3, 20),
            current_balance_overrides={"контрагент b": Decimal("33.00")},
            strict_current_balance_overrides=True,
        )
        session.commit()

        snapshots = session.query(ReceivableReconciliationSnapshot).all()
        total = sum((snapshot.signed_balance for snapshot in snapshots), Decimal("0.00"))

        assert result["reconciliation_snapshots"] == 1
        assert result["overrides_applied"] == 1
        assert len(snapshots) == 1
        assert total == Decimal("33.00")


def test_build_receivable_cases_keeps_signed_buyers_but_skips_negative_debt_cases() -> None:
    app_engine = create_engine("sqlite:///:memory:")
    onec_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(app_engine)
    _setup_onec_source(onec_engine)

    extractor = OneCReceivableLedgerExtractor(onec_engine, operations_sql=NORMALIZED_SQL)
    events = extractor.fetch_receivable_events()

    with Session(app_engine) as session:
        sync_receivable_ledger(session, events)
        build_receivable_balance_snapshots(
            session,
            snapshot_date=date(2026, 3, 20),
            current_balance_overrides={"контрагент c": Decimal("-25.00")},
        )
        result = build_receivable_cases(
            session,
            snapshot_date=date(2026, 3, 20),
            employee_counterparty_refs=["cp-b"],
            fired_manager_refs=["mgr-4"],
        )
        session.commit()

        cases = (
            session.query(ReceivableCase)
            .order_by(ReceivableCase.segment, ReceivableCase.counterparty_ref)
            .all()
        )

        assert result["segments"]["buyers"] == 3
        assert result["segments"]["new_daily"] == 1
        assert result["segments"]["employee"] == 1
        assert result["segments"]["overdue"] == 1
        assert result["segments"].get("inactive", 0) == 0
        assert result["segments"].get("fired_manager", 0) == 0
        assert result["segments"].get("adjustment_candidates", 0) == 0
        assert [(item.segment, item.counterparty_ref) for item in cases] == [
            ("buyers", "cp-a"),
            ("buyers", "cp-c"),
            ("buyers", "cp-d"),
            ("employee", "cp-b"),
            ("new_daily", "cp-d"),
            ("overdue", "cp-b"),
        ]
        cp_c_buyer = next(
            item for item in cases if item.segment == "buyers" and item.counterparty_ref == "cp-c"
        )
        assert cp_c_buyer.current_balance == Decimal("-25.00")


def test_build_receivable_cases_creates_required_segments() -> None:
    app_engine = create_engine("sqlite:///:memory:")
    onec_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(app_engine)
    _setup_onec_source(onec_engine)

    extractor = OneCReceivableLedgerExtractor(onec_engine, operations_sql=NORMALIZED_SQL)
    events = extractor.fetch_receivable_events()

    with Session(app_engine) as session:
        sync_receivable_ledger(session, events)
        build_receivable_balance_snapshots(session, snapshot_date=date(2026, 3, 20))
        result = build_receivable_cases(
            session,
            snapshot_date=date(2026, 3, 20),
            employee_counterparty_refs=["cp-b"],
            fired_manager_refs=["mgr-4"],
        )
        session.commit()

        assert result["segments"]["buyers"] == 3
        assert result["segments"]["new_daily"] == 1
        assert result["segments"]["employee"] == 1
        assert result["segments"]["overdue"] == 2
        assert result["segments"]["fired_manager"] == 1
        assert result["segments"]["inactive"] == 1
        assert result["segments"]["adjustment_candidates"] == 1

        cases = (
            session.query(ReceivableCase)
            .order_by(ReceivableCase.segment, ReceivableCase.counterparty_ref)
            .all()
        )
        assert len(cases) == 10

        buyers = [item.counterparty_ref for item in cases if item.segment == "buyers"]
        assert buyers == ["cp-a", "cp-c", "cp-d"]

        new_daily = next(item for item in cases if item.segment == "new_daily")
        assert new_daily.counterparty_ref == "cp-d"
        assert new_daily.owner_type == "current_manager"

        employee = next(item for item in cases if item.segment == "employee")
        assert employee.counterparty_ref == "cp-b"
        assert employee.owner_type == "finance_hr"
        assert employee.is_overdue is True
        assert employee.due_date == datetime(2026, 2, 10, 0, 0, 0)

        fired = next(item for item in cases if item.segment == "fired_manager")
        assert fired.counterparty_ref == "cp-c"
        assert fired.owner_type == "finance_pool"
        assert fired.shipment_ban is True

        overdue = [item.counterparty_ref for item in cases if item.segment == "overdue"]
        assert overdue == ["cp-b", "cp-c"]

        adjustment = next(item for item in cases if item.segment == "adjustment_candidates")
        assert adjustment.counterparty_ref == "cp-c"
        assert adjustment.chain_documents[0]["document_ref"] == "sale-c1"


def _add_receivable_snapshot(
    session: Session,
    *,
    snapshot_date: date,
    counterparty_ref: str,
    counterparty_name: str,
    current_balance: Decimal,
    origin_document_date: datetime,
) -> None:
    session.add(
        ReceivableBalanceSnapshot(
            snapshot_date=snapshot_date,
            counterparty_ref=counterparty_ref,
            counterparty_name=counterparty_name,
            current_balance=current_balance,
            origin_document_ref=f"sale-{counterparty_ref}",
            origin_document_number=f"S-{counterparty_ref}",
            origin_document_date=origin_document_date,
            origin_manager_ref="mgr-1",
            origin_manager_name="Менеджер 1",
            current_manager_ref="mgr-1",
            current_manager_name="Менеджер 1",
            aged_bucket="0-7",
            activity_segment="active",
            payment_term_source="missing",
            is_overdue=False,
            shipment_ban=False,
        )
    )


def test_build_receivable_cases_skips_new_daily_when_balance_did_not_grow_vs_previous_day() -> None:
    app_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(app_engine)

    with Session(app_engine) as session:
        session.add(
            ReceivableBalanceSnapshot(
                snapshot_date=date(2026, 3, 19),
                counterparty_ref="cp-same",
                counterparty_name="Контрагент без роста",
                current_balance=Decimal("100.00"),
                origin_document_ref="sale-old",
                origin_document_number="S-OLD",
                origin_document_date=datetime(2026, 3, 1, 10, 0, 0),
                origin_manager_ref="mgr-1",
                origin_manager_name="Менеджер 1",
                current_manager_ref="mgr-1",
                current_manager_name="Менеджер 1",
                aged_bucket="8-30",
                activity_segment="active",
                payment_term_source="missing",
                is_overdue=False,
                shipment_ban=False,
            )
        )
        session.add(
            ReceivableBalanceSnapshot(
                snapshot_date=date(2026, 3, 20),
                counterparty_ref="cp-same",
                counterparty_name="Контрагент без роста",
                current_balance=Decimal("100.00"),
                origin_document_ref="sale-new",
                origin_document_number="S-NEW",
                origin_document_date=datetime(2026, 3, 16, 10, 0, 0),
                origin_manager_ref="mgr-1",
                origin_manager_name="Менеджер 1",
                current_manager_ref="mgr-1",
                current_manager_name="Менеджер 1",
                aged_bucket="0-7",
                activity_segment="active",
                payment_term_source="missing",
                is_overdue=False,
                shipment_ban=False,
            )
        )

        result = build_receivable_cases(session, snapshot_date=date(2026, 3, 20))
        session.commit()

        assert result["segments"]["buyers"] == 1
        assert result["segments"].get("new_daily", 0) == 0
        assert result["segments"].get("overdue", 0) == 0
        cases = session.query(ReceivableCase).all()
        assert len(cases) == 1
        assert cases[0].segment == "buyers"
        assert cases[0].counterparty_ref == "cp-same"


def test_build_receivable_cases_skips_new_daily_inside_three_day_grace() -> None:
    app_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(app_engine)

    with Session(app_engine) as session:
        _add_receivable_snapshot(
            session,
            snapshot_date=date(2026, 3, 20),
            counterparty_ref="cp-grace",
            counterparty_name="Контрагент в льготном окне",
            current_balance=Decimal("100.00"),
            origin_document_date=datetime(2026, 3, 17, 10, 0, 0),
        )

        result = build_receivable_cases(session, snapshot_date=date(2026, 3, 20))
        session.commit()

        assert result["segments"]["buyers"] == 1
        assert result["segments"].get("new_daily", 0) == 0
        cases = session.query(ReceivableCase).all()
        assert len(cases) == 1
        assert cases[0].segment == "buyers"
        assert cases[0].counterparty_ref == "cp-grace"


def test_build_receivable_cases_skips_new_daily_when_prepayment_covers_sale() -> None:
    app_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(app_engine)

    with Session(app_engine) as session:
        _add_receivable_snapshot(
            session,
            snapshot_date=date(2026, 3, 19),
            counterparty_ref="cp-prepaid",
            counterparty_name="Контрагент с предоплатой",
            current_balance=Decimal("-50.00"),
            origin_document_date=datetime(2026, 3, 1, 10, 0, 0),
        )
        _add_receivable_snapshot(
            session,
            snapshot_date=date(2026, 3, 20),
            counterparty_ref="cp-prepaid",
            counterparty_name="Контрагент с предоплатой",
            current_balance=Decimal("-10.00"),
            origin_document_date=datetime(2026, 3, 16, 10, 0, 0),
        )

        result = build_receivable_cases(session, snapshot_date=date(2026, 3, 20))
        session.commit()

        assert result["segments"]["buyers"] == 1
        assert result["segments"].get("new_daily", 0) == 0
        cases = session.query(ReceivableCase).all()
        assert len(cases) == 1
        assert cases[0].segment == "buyers"
        assert cases[0].current_balance == Decimal("-10.00")


def test_build_receivable_cases_limits_adjustment_candidates_to_buyers_pool() -> None:
    app_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(app_engine)

    with Session(app_engine) as session:
        for counterparty_ref, counterparty_name in (
            ("cp-buyer", "Покупатель"),
            ("cp-employee", "Сотрудник"),
            ("cp-other", "Прочий контрагент"),
        ):
            session.add(
                ReceivableBalanceSnapshot(
                    snapshot_date=date(2026, 3, 20),
                    counterparty_ref=counterparty_ref,
                    counterparty_name=counterparty_name,
                    current_balance=Decimal("100.00"),
                    origin_document_ref=f"sale-{counterparty_ref}",
                    origin_document_number=f"S-{counterparty_ref}",
                    origin_document_date=datetime(2026, 1, 10, 10, 0, 0),
                    origin_manager_ref="mgr-1",
                    origin_manager_name="Менеджер 1",
                    current_manager_ref="mgr-1",
                    current_manager_name="Менеджер 1",
                    aged_bucket="90+",
                    activity_segment="inactive",
                    payment_term_source="missing",
                    is_overdue=True,
                    overdue_days=40,
                    shipment_ban=False,
                )
            )

        result = build_receivable_cases(
            session,
            snapshot_date=date(2026, 3, 20),
            employee_counterparty_refs=["cp-employee"],
            buyer_counterparty_refs=["cp-buyer"],
        )
        session.commit()

        assert result["segments"]["inactive"] == 3
        assert result["segments"]["employee"] == 1
        assert result["segments"]["buyers"] == 1
        assert result["segments"]["adjustment_candidates"] == 1

        adjustment_refs = [
            item.counterparty_ref
            for item in session.query(ReceivableCase)
            .filter(ReceivableCase.segment == "adjustment_candidates")
            .order_by(ReceivableCase.counterparty_ref)
            .all()
        ]
        assert adjustment_refs == ["cp-buyer"]


def test_build_receivable_cases_auto_detects_fired_manager_from_staff_directory() -> None:
    app_engine = create_engine("sqlite:///:memory:")
    onec_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(app_engine)
    _setup_onec_source(onec_engine)

    extractor = OneCReceivableLedgerExtractor(onec_engine, operations_sql=NORMALIZED_SQL)
    events = extractor.fetch_receivable_events()

    with Session(app_engine) as session:
        sync_receivable_ledger(session, events)
        build_receivable_balance_snapshots(session, snapshot_date=date(2026, 3, 20))
        session.add(
            StaffMember(
                source="b24_hr",
                external_ref="staff-terminated-1",
                full_name="Менеджер 4",
                role_code="sales_manager",
                role_name="Менеджер продаж",
                department_ref="dep-1",
                department_name="Продажи",
                store_ref=None,
                store_name=None,
                employment_status="fired",
                hire_date=None,
                termination_date=date(2026, 3, 1),
                manager_ref=None,
                manager_name=None,
            )
        )
        result = build_receivable_cases(session, snapshot_date=date(2026, 3, 20))
        session.commit()

        assert result["segments"]["fired_manager"] == 1
        fired = (
            session.query(ReceivableCase).filter(ReceivableCase.segment == "fired_manager").one()
        )
        assert fired.counterparty_ref == "cp-c"
