from __future__ import annotations

import inspect
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.models import (
    Base,
    ReceivableBalanceSnapshot,
    ReceivableCase,
    ReceivableLedgerEvent,
)
from app.services.receivables import (
    AuthoritativeReceivableBalanceRow,
    OneCReceivableLedgerExtractor,
    _fetch_canonical_summary_current_balance_rows_from_onec,
    _fetch_open_debt_managers_from_onec,
    fetch_current_balances_from_onec,
)
from app.services.receivables_extractors import (
    PAYMENTS_SQL,
    RECEIVABLE_LAYER_EMPLOYEE_MOVEMENTS,
    RECEIVABLE_LAYER_EMPLOYEE_OPENING,
    RECEIVABLE_LAYER_PAYMENTS,
    RECEIVABLE_LAYER_REGULAR_OPENING,
    RECEIVABLE_LAYER_SALES_RETURNS,
    RECEIVABLE_LAYER_SETTLEMENTS,
    REGULAR_OPENING_SQL,
)
from app.workers import receivables as receivables_worker
from app.workers.receivables import (
    _build_receivable_sync_windows,
    _snapshot_window_with_lookback,
    run_receivable_daily_events_sync,
    run_receivable_history_backfill,
    run_receivable_ledger_sync,
    run_receivable_read_model_rebuild,
)
from tests.test_receivables import _setup_onec_source

REGULAR_OPENING_LAYER_SQL = """
SELECT
    'onec-layer-test' AS source,
    'opening_balance' AS event_type,
    'opening-regular' AS external_document_ref,
    'Opening Regular' AS external_document_number,
    :opening_balance_date || 'T00:00:00' AS external_document_date,
    'cp-open-regular' AS counterparty_ref,
    'Контрагент opening regular' AS counterparty_name,
    'contract-open-regular' AS contract_ref,
    'Opening Contract Regular' AS contract_name,
    'kind-buyer' AS contract_kind_ref,
    'С покупателем' AS contract_kind_name,
    NULL AS manager_ref,
    NULL AS manager_name,
    NULL AS store_ref,
    NULL AS store_name,
    'regular_receivables' AS source_layer,
    NULL AS planned_payment_date,
    NULL AS credit_depth_days,
    0 AS shipment_ban,
    1 AS line_no,
    25 AS amount_delta,
    0 AS skip_ingest
WHERE :opening_balance_date IS NOT NULL
"""

EMPLOYEE_OPENING_LAYER_SQL = """
SELECT
    'onec-layer-test' AS source,
    'opening_balance' AS event_type,
    'opening-employee' AS external_document_ref,
    'Opening Employee' AS external_document_number,
    :opening_balance_date || 'T00:00:00' AS external_document_date,
    'cp-open-employee' AS counterparty_ref,
    'Контрагент opening employee' AS counterparty_name,
    'contract-open-employee' AS contract_ref,
    'Opening Contract Employee' AS contract_name,
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
    15 AS amount_delta,
    0 AS skip_ingest
WHERE :opening_balance_date IS NOT NULL
"""

SALES_RETURNS_LAYER_SQL = """
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
WHERE event_type IN ('sale', 'return')
  AND source_layer = 'regular_receivables'
  AND (:window_start IS NULL OR external_document_date >= :window_start)
  AND (:window_end IS NULL OR external_document_date < :window_end)
ORDER BY external_document_date, line_no
"""

PAYMENTS_LAYER_SQL = """
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
WHERE event_type = 'payment'
  AND source_layer = 'regular_receivables'
  AND (:window_start IS NULL OR external_document_date >= :window_start)
  AND (:window_end IS NULL OR external_document_date < :window_end)
ORDER BY external_document_date, line_no
"""

SETTLEMENTS_LAYER_SQL = """
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
WHERE event_type = 'settlement'
  AND source_layer = 'regular_receivables'
  AND (:window_start IS NULL OR external_document_date >= :window_start)
  AND (:window_end IS NULL OR external_document_date < :window_end)
ORDER BY external_document_date, line_no
"""

EMPLOYEE_MOVEMENTS_LAYER_SQL = """
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
WHERE event_type = 'debt_adjustment'
  AND source_layer = 'employee_summary'
  AND (:window_start IS NULL OR external_document_date >= :window_start)
  AND (:window_end IS NULL OR external_document_date < :window_end)
ORDER BY external_document_date, line_no
"""

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


def _build_fake_layer_extractors(onec_engine):
    return {
        RECEIVABLE_LAYER_REGULAR_OPENING: OneCReceivableLedgerExtractor(
            onec_engine,
            operations_sql=REGULAR_OPENING_LAYER_SQL,
        ),
        RECEIVABLE_LAYER_EMPLOYEE_OPENING: OneCReceivableLedgerExtractor(
            onec_engine,
            operations_sql=EMPLOYEE_OPENING_LAYER_SQL,
        ),
        RECEIVABLE_LAYER_SALES_RETURNS: OneCReceivableLedgerExtractor(
            onec_engine,
            operations_sql=SALES_RETURNS_LAYER_SQL,
        ),
        RECEIVABLE_LAYER_PAYMENTS: OneCReceivableLedgerExtractor(
            onec_engine,
            operations_sql=PAYMENTS_LAYER_SQL,
        ),
        RECEIVABLE_LAYER_SETTLEMENTS: OneCReceivableLedgerExtractor(
            onec_engine,
            operations_sql=SETTLEMENTS_LAYER_SQL,
        ),
        RECEIVABLE_LAYER_EMPLOYEE_MOVEMENTS: OneCReceivableLedgerExtractor(
            onec_engine,
            operations_sql=EMPLOYEE_MOVEMENTS_LAYER_SQL,
        ),
    }


def _setup_layered_onec_source(engine) -> None:
    _setup_onec_source(engine)
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO onec_receivable_source (
                source, event_type, external_document_ref, external_document_number,
                external_document_date, counterparty_ref, counterparty_name,
                contract_ref, contract_name, contract_kind_ref, contract_kind_name,
                manager_ref, manager_name, store_ref, store_name,
                source_layer, planned_payment_date, credit_depth_days, shipment_ban,
                line_no, amount_delta
            ) VALUES
                (
                    'onec', 'payment', 'pay-a2', 'P-002', '2026-03-20 11:00:00',
                    'cp-a', 'Контрагент A', 'contract-a', 'Основной договор A',
                    'kind-buyer', 'С покупателем', 'mgr-2', 'Менеджер 2',
                    'store-1', 'Магазин 1', 'regular_receivables',
                    '2026-03-25 00:00:00', 7, 0, 1, -20
                ),
                (
                    'onec', 'settlement', 'set-a2', 'Z-002', '2026-03-20 12:00:00',
                    'cp-a', 'Контрагент A', 'contract-a', 'Основной договор A',
                    'kind-buyer', 'С покупателем', NULL, NULL,
                    'store-1', 'Магазин 1', 'regular_receivables',
                    '2026-03-25 00:00:00', 7, 0, 1, -5
                ),
                (
                    'onec', 'debt_adjustment', 'adj-b2', 'A-002', '2026-03-20 13:00:00',
                    'cp-b', 'Контрагент B', 'contract-b', 'Основной договор B',
                    'kind-employee', 'С покупателем', NULL, NULL,
                    'store-2', 'Магазин 2', 'employee_summary',
                    '2026-03-20 00:00:00', NULL, 0, 1, -10
                )
        """))


def test_build_sync_windows_applies_snapshot_end_without_window_start() -> None:
    snapshot_date = date(2026, 2, 28)

    windows = _build_receivable_sync_windows(
        window_start=None,
        window_end=None,
        snapshot_date=snapshot_date,
    )

    assert windows == [(None, datetime.combine(snapshot_date + timedelta(days=1), time.min))]


def test_snapshot_window_with_lookback_expands_start_date() -> None:
    snapshot_date = date(2026, 2, 28)

    window_start, window_end = _snapshot_window_with_lookback(snapshot_date, window_days=7)

    assert window_start == datetime(2026, 2, 22, 0, 0, 0)
    assert window_end == datetime(2026, 3, 1, 0, 0, 0)


def test_run_receivable_daily_events_sync_loads_layers_without_read_model_rebuild(
    monkeypatch,
) -> None:
    app_engine = create_engine("sqlite:///:memory:")
    onec_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(app_engine)
    _setup_layered_onec_source(onec_engine)
    monkeypatch.setattr(
        receivables_worker,
        "build_receivable_layer_extractors",
        _build_fake_layer_extractors,
    )
    monkeypatch.setattr(
        "app.services.receivables_extractors.build_receivable_layer_extractors",
        _build_fake_layer_extractors,
    )
    monkeypatch.setattr(
        "app.services.receivables._authoritative_layer_opening_dates",
        lambda *_args, **_kwargs: {
            "regular_opening": date(2026, 2, 28),
            "employee_opening": date(2026, 3, 19),
        },
    )
    monkeypatch.setattr(
        "app.services.receivables_extractors.build_receivable_layer_extractors",
        _build_fake_layer_extractors,
    )

    result = run_receivable_daily_events_sync(
        snapshot_date=date(2026, 3, 20),
        employee_counterparty_refs=("cp-b",),
        onec_engine=onec_engine,
        app_engine=app_engine,
    )

    assert result["processed"] == 3
    assert result["inserted"] == 3
    assert result["snapshots"] == 0
    assert set(result["layers"]) == {
        "sales_returns",
        "payments",
        "settlements",
        "employee_movements",
    }

    with Session(app_engine) as session:
        assert session.query(ReceivableLedgerEvent).count() == 3
        assert session.query(ReceivableBalanceSnapshot).count() == 0
        assert session.query(ReceivableCase).count() == 0


def test_run_receivable_daily_events_sync_uses_window_days_lookback(
    monkeypatch,
) -> None:
    app_engine = create_engine("sqlite:///:memory:")
    onec_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(app_engine)
    _setup_layered_onec_source(onec_engine)
    monkeypatch.setattr(
        receivables_worker,
        "build_receivable_layer_extractors",
        _build_fake_layer_extractors,
    )

    result = run_receivable_daily_events_sync(
        snapshot_date=date(2026, 3, 20),
        window_days=6,
        employee_counterparty_refs=("cp-b",),
        onec_engine=onec_engine,
        app_engine=app_engine,
    )

    assert result["processed"] == 5

    with Session(app_engine) as session:
        events = (
            session.execute(select(ReceivableLedgerEvent).order_by(ReceivableLedgerEvent.id))
            .scalars()
            .all()
        )
        assert len(events) == 5
        assert any(item.external_document_number == "R-001" for item in events)
        assert any(item.external_document_number == "S-301" for item in events)
        assert any(item.external_document_number == "P-002" for item in events)


def test_run_receivable_read_model_rebuild_builds_snapshots_and_cases_from_layered_ledger(
    monkeypatch,
) -> None:
    app_engine = create_engine("sqlite:///:memory:")
    onec_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(app_engine)
    _setup_layered_onec_source(onec_engine)
    monkeypatch.setattr(
        receivables_worker,
        "build_receivable_layer_extractors",
        _build_fake_layer_extractors,
    )
    monkeypatch.setattr(
        "app.services.receivables_extractors.build_receivable_layer_extractors",
        _build_fake_layer_extractors,
    )
    monkeypatch.setattr(
        "app.services.receivables._authoritative_layer_opening_dates",
        lambda *_args, **_kwargs: {
            "regular_opening": date(2026, 2, 28),
            "employee_opening": date(2026, 3, 19),
        },
    )
    monkeypatch.setattr(
        receivables_worker,
        "_resolve_buyer_counterparty_refs",
        lambda *_args, **_kwargs: (),
    )

    run_receivable_daily_events_sync(
        snapshot_date=date(2026, 3, 20),
        window_days=6,
        employee_counterparty_refs=("cp-b",),
        onec_engine=onec_engine,
        app_engine=app_engine,
    )

    result = run_receivable_read_model_rebuild(
        snapshot_date=date(2026, 3, 20),
        employee_counterparty_refs=("cp-b",),
        staff_rows=[],
        require_seeded_ledger=False,
        onec_engine=onec_engine,
        app_engine=app_engine,
    )

    assert result["assignments"] >= 1
    assert result["snapshots"] >= 1
    assert result["cases"] >= 1

    with Session(app_engine) as session:
        snapshots = (
            session.execute(
                select(ReceivableBalanceSnapshot).where(
                    ReceivableBalanceSnapshot.snapshot_date == date(2026, 3, 20)
                )
            )
            .scalars()
            .all()
        )
        assert snapshots
        assert any(item.counterparty_ref == "cp-d" for item in snapshots)

        cases = (
            session.execute(
                select(ReceivableCase).where(ReceivableCase.snapshot_date == date(2026, 3, 20))
            )
            .scalars()
            .all()
        )
        assert cases


def test_resolve_buyer_counterparty_refs_prefers_onec_group_over_local_ledger(
    monkeypatch,
) -> None:
    app_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(app_engine)
    with Session(app_engine) as session:
        session.add(
            ReceivableLedgerEvent(
                business_key="local-ledger-buyer",
                event_type="sale",
                external_document_ref="sale-local",
                external_document_number="S-LOCAL",
                external_document_date=datetime(2026, 3, 20, 10, 0, 0),
                counterparty_ref="cp-local-contract-kind",
                counterparty_name="Локальный договорный покупатель",
                contract_kind_name="С покупателем",
                amount_delta=Decimal("100.00"),
            )
        )
        session.commit()

    monkeypatch.setattr(
        receivables_worker,
        "fetch_counterparty_refs_from_onec_group",
        lambda *_args, **_kwargs: ("cp-onec-group",),
    )

    refs = receivables_worker._resolve_buyer_counterparty_refs(
        object(),
        app_engine=app_engine,
    )

    assert refs == ("cp-onec-group",)


def test_run_receivable_read_model_rebuild_uses_authoritative_balance_rows(monkeypatch) -> None:
    app_engine = create_engine("sqlite:///:memory:")
    onec_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(app_engine)
    _setup_layered_onec_source(onec_engine)
    monkeypatch.setattr(
        receivables_worker,
        "build_receivable_layer_extractors",
        _build_fake_layer_extractors,
    )
    monkeypatch.setattr(
        receivables_worker,
        "_resolve_authoritative_balance_rows",
        lambda **_kwargs: (
            [
                AuthoritativeReceivableBalanceRow(
                    counterparty_ref="cp-b",
                    counterparty_name="Контрагент B",
                    current_balance=Decimal("33.00"),
                    source="test_authoritative",
                ),
                AuthoritativeReceivableBalanceRow(
                    counterparty_ref="cp-d",
                    counterparty_name="Контрагент D",
                    current_balance=Decimal("70.00"),
                    source="test_authoritative",
                ),
            ],
            {
                "regular_current_override_count": 0,
                "current_import_override_count": 0,
                "total_current_override_count": 0,
                "employee_current_import_override_count": 0,
                "authoritative_balance_row_count": 2,
                "balance_source_mode": "authoritative_from_onec_daily_extractor",
            },
        ),
    )
    monkeypatch.setattr(
        receivables_worker, "_resolve_buyer_counterparty_refs", lambda *_args, **_kwargs: ()
    )

    run_receivable_daily_events_sync(
        snapshot_date=date(2026, 3, 20),
        window_days=6,
        employee_counterparty_refs=("cp-b",),
        onec_engine=onec_engine,
        app_engine=app_engine,
    )

    result = run_receivable_read_model_rebuild(
        snapshot_date=date(2026, 3, 20),
        employee_counterparty_refs=("cp-b",),
        staff_rows=[],
        require_seeded_ledger=False,
        onec_engine=onec_engine,
        app_engine=app_engine,
    )

    assert result["authoritative_balance_row_count"] == 2
    assert result["balance_source_mode"] == "authoritative_from_onec_daily_extractor"

    with Session(app_engine) as session:
        employee_snapshot = (
            session.execute(
                select(ReceivableBalanceSnapshot).where(
                    ReceivableBalanceSnapshot.snapshot_date == date(2026, 3, 20),
                    ReceivableBalanceSnapshot.counterparty_ref == "cp-b",
                )
            )
            .scalars()
            .one()
        )
        assert employee_snapshot.current_balance == 33

        regular_snapshot = (
            session.execute(
                select(ReceivableBalanceSnapshot).where(
                    ReceivableBalanceSnapshot.snapshot_date == date(2026, 3, 20),
                    ReceivableBalanceSnapshot.counterparty_ref == "cp-d",
                )
            )
            .scalars()
            .one()
        )
        assert regular_snapshot.current_balance == 70

        employee_case = (
            session.execute(
                select(ReceivableCase).where(
                    ReceivableCase.snapshot_date == date(2026, 3, 20),
                    ReceivableCase.segment == "employee",
                    ReceivableCase.counterparty_ref == "cp-b",
                )
            )
            .scalars()
            .one()
        )
        assert employee_case.current_balance == 33


def test_fetch_current_balances_from_onec_uses_onec_daily_extractor(monkeypatch) -> None:
    onec_engine = create_engine("sqlite:///:memory:")
    _setup_layered_onec_source(onec_engine)
    with onec_engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO onec_receivable_source (
                source, event_type, external_document_ref, external_document_number,
                external_document_date, counterparty_ref, counterparty_name,
                contract_ref, contract_name, contract_kind_ref, contract_kind_name,
                manager_ref, manager_name, store_ref, store_name,
                source_layer, planned_payment_date, credit_depth_days, shipment_ban,
                line_no, amount_delta
            ) VALUES
                (
                    'onec', 'payment', 'pay-b-regular', 'P-B-001', '2026-03-20 09:00:00',
                    'cp-b', 'Контрагент B', 'contract-b', 'Основной договор B',
                    'kind-buyer', 'С покупателем', NULL, NULL,
                    'store-2', 'Магазин 2', 'regular_receivables',
                    NULL, NULL, 0, 1, -999
                ),
                (
                    'onec', 'debt_adjustment', 'adj-b-old', 'A-B-OLD', '2026-03-10 09:00:00',
                    'cp-b', 'Контрагент B', 'contract-b', 'Основной договор B',
                    'kind-employee', 'С покупателем', NULL, NULL,
                    'store-2', 'Магазин 2', 'employee_summary',
                    NULL, NULL, 0, 1, 777
                ),
                (
                    'onec', 'debt_adjustment', 'adj-b-anchor', 'A-B-ANCHOR', '2026-03-19 09:00:00',
                    'cp-b', 'Контрагент B', 'contract-b', 'Основной договор B',
                    'kind-employee', 'С покупателем', NULL, NULL,
                    'store-2', 'Магазин 2', 'employee_summary',
                    NULL, NULL, 0, 1, -5
                )
        """))
    monkeypatch.setattr(
        "app.services.receivables_extractors.build_receivable_layer_extractors",
        _build_fake_layer_extractors,
    )
    monkeypatch.setattr(
        "app.services.receivables._authoritative_layer_opening_dates",
        lambda *_args, **_kwargs: {
            "regular_opening": date(2026, 2, 28),
            "employee_opening": date(2026, 3, 19),
        },
    )

    rows, meta = fetch_current_balances_from_onec(
        onec_engine,
        snapshot_date=date(2026, 3, 20),
        employee_counterparty_refs=("cp-b", "cp-open-employee"),
    )

    assert meta["balance_source_mode"] == "authoritative_from_onec_daily_extractor"
    assert meta["opening_balance_date"] == date(2026, 2, 28)
    assert meta["opening_balance_dates"] == [date(2026, 2, 28), date(2026, 3, 19)]
    assert meta["regular_opening_balance_date"] == date(2026, 2, 28)
    assert meta["employee_opening_balance_date"] == date(2026, 3, 19)
    assert meta["opening_row_count"] == 2
    assert meta["daily_movement_row_count"] == 9

    by_ref = {row.counterparty_ref: row for row in rows}
    assert by_ref["cp-a"].current_balance == Decimal("55.00")
    assert by_ref["cp-a"].current_manager_ref == "mgr-2"
    assert by_ref["cp-b"].current_balance == Decimal("-15.00")
    assert by_ref["cp-d"].current_balance == Decimal("70.00")
    assert by_ref["cp-open-regular"].current_balance == Decimal("25.00")
    assert by_ref["cp-open-employee"].current_balance == Decimal("15.00")


def test_regular_opening_sql_defines_employee_counterparties_cte() -> None:
    assert "employee_counterparties AS" in REGULAR_OPENING_SQL
    assert "LEFT JOIN employee_counterparties AS employee" in REGULAR_OPENING_SQL


def test_primary_payment_sql_does_not_filter_contract_kind() -> None:
    assert "contract_kind._Fld515RRef" not in PAYMENTS_SQL
    assert (
        "0x9363c6f0a10557bf4822a55db4862286"
        not in PAYMENTS_SQL.split("regular_summary_extra_register AS", maxsplit=1)[1]
    )


def test_canonical_summary_uses_full_mutual_statement_register() -> None:
    source = inspect.getsource(_fetch_canonical_summary_current_balance_rows_from_onec)
    assert "_AccumRgT7009" in source
    assert "_AccumRg7002" in source
    assert "r._Fld7008" in source
    assert "CROSS JOIN latest_opening_period AS p" in source
    assert "r._Period >= p.period" in source
    assert "r._Fld7621" not in source
    assert "_AccumRg7614" not in source
    assert "_fetch_open_debt_managers_from_onec" in source


def test_open_debt_manager_uses_sale_that_opened_current_positive_balance() -> None:
    onec_engine = create_engine("sqlite:///:memory:")
    with onec_engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE _AccumRg7002 (
                _Active INTEGER,
                _Fld7006RRef TEXT,
                _RecorderTRef TEXT,
                _RecorderRRef TEXT,
                _Period TEXT,
                _LineNo INTEGER,
                _RecordKind INTEGER,
                _Fld7008 NUMERIC
            )
        """))
        conn.execute(text("""
            CREATE TABLE _Document203 (
                _IDRRef TEXT,
                _Fld4950RRef TEXT
            )
        """))
        conn.execute(text("""
            CREATE TABLE _Reference69 (
                _IDRRef TEXT,
                _Description TEXT
            )
        """))
        conn.execute(text("""
            INSERT INTO _Reference69 VALUES
                ('mgr-open', 'Менеджер открытого долга'),
                ('mgr-late', 'Менеджер поздней реализации')
        """))
        conn.execute(text("""
            INSERT INTO _Document203 VALUES
                ('sale-open', 'mgr-open'),
                ('sale-late', 'mgr-late')
        """))
        conn.execute(text("""
            INSERT INTO _AccumRg7002 VALUES
                (1, 'cp-a', '0x000000CB', 'sale-open', '2026-01-10 10:00:00', 1, 0, 11500),
                (1, 'cp-a', '0x000000CB', 'sale-late', '2026-02-10 10:00:00', 1, 0, 300),
                (1, 'cp-a', '0x000000C4', 'pay-late', '2026-02-10 10:01:00', 1, 1, 300)
        """))

    try:
        result = _fetch_open_debt_managers_from_onec(
            onec_engine,
            counterparty_refs=("cp-a",),
            movement_end=datetime(2026, 3, 1),
        )
    finally:
        onec_engine.dispose()

    assert result["cp-a"] == ("mgr-open", "Менеджер открытого долга")


def test_resolve_authoritative_balance_rows_uses_onec_current_balances(monkeypatch) -> None:
    expected_rows = [
        AuthoritativeReceivableBalanceRow(
            counterparty_ref="cp-a",
            counterparty_name="Контрагент A",
            current_balance=Decimal("42.00"),
            source="test",
        )
    ]

    def fake_fetch(onec_engine, **kwargs):
        assert onec_engine is onec_engine_marker
        assert kwargs["snapshot_date"] == date(2026, 3, 20)
        assert kwargs["employee_counterparty_refs"] == ("employee-cp",)
        return expected_rows, {
            "authoritative_balance_row_count": 1,
            "balance_source_mode": "onec_canonical_mutual_statement_7002",
        }

    onec_engine_marker = object()
    monkeypatch.setattr(receivables_worker, "fetch_current_balances_from_onec", fake_fetch)

    rows, meta = receivables_worker._resolve_authoritative_balance_rows(
        onec_engine=onec_engine_marker,
        app_engine=None,
        snapshot_date=date(2026, 3, 20),
        employee_counterparty_refs=("employee-cp",),
        operations_sql=None,
        opening_balance_date=None,
        opening_import_path=None,
        opening_snapshot_date=None,
        current_import_path=None,
        current_import_counterparty_group="ПОКУПАТЕЛИ",
        employee_current_import_path=None,
        employee_current_import_counterparty_group="СОТРУДНИКИ",
    )

    assert rows == expected_rows
    assert meta["balance_source_mode"] == "onec_canonical_mutual_statement_7002"
    assert meta["authoritative_balance_row_count"] == 1


def test_resolve_authoritative_balance_rows_rejects_projection_args() -> None:
    with pytest.raises(ValueError, match="сначала загрузите seed и движения"):
        receivables_worker._resolve_authoritative_balance_rows(
            onec_engine=object(),
            app_engine=None,
            snapshot_date=date(2026, 4, 19),
            employee_counterparty_refs=(),
            operations_sql="SELECT movements",
            opening_balance_date=date(2025, 1, 1),
            opening_import_path="docs/ВзаиморасчетыВсе.normalized.csv",
            opening_snapshot_date=None,
            current_import_path=None,
            current_import_counterparty_group="ПОКУПАТЕЛИ",
            employee_current_import_path=None,
            employee_current_import_counterparty_group="СОТРУДНИКИ",
        )


def test_resolve_authoritative_balance_rows_rejects_snapshot_seed() -> None:
    with pytest.raises(ValueError, match="opening_snapshot_date больше не поддерживается"):
        receivables_worker._resolve_authoritative_balance_rows(
            onec_engine=object(),
            app_engine=object(),
            snapshot_date=date(2026, 4, 19),
            employee_counterparty_refs=(),
            operations_sql="SELECT 1",
            opening_balance_date=date(2026, 4, 1),
            opening_import_path=None,
            opening_snapshot_date=date(2026, 3, 31),
            current_import_path=None,
            current_import_counterparty_group="ПОКУПАТЕЛИ",
            employee_current_import_path=None,
            employee_current_import_counterparty_group="СОТРУДНИКИ",
        )


def test_project_authoritative_balance_rows_from_onec_supports_cte_sql() -> None:
    onec_engine = create_engine("sqlite:///:memory:")
    _setup_onec_source(onec_engine)

    cte_sql = """
WITH receivable_events_base AS (
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
        amount_delta,
        0 AS skip_ingest
    FROM onec_receivable_source

    UNION ALL

    SELECT
        'onec',
        'sale',
        'skip-a',
        'S-SKIP',
        '2026-03-20 18:00:00',
        'cp-a',
        'Контрагент A skip',
        'contract-a',
        'Основной договор A',
        'kind-buyer',
        'С покупателем',
        'mgr-skip',
        'Менеджер skip',
        'store-1',
        'Магазин 1',
        'regular_receivables',
        '2026-03-25 00:00:00',
        7,
        0,
        9,
        999,
        1
)
SELECT *
FROM receivable_events_base
WHERE (:window_start IS NULL OR external_document_date >= :window_start)
  AND (:window_end IS NULL OR external_document_date < :window_end)
ORDER BY external_document_date, line_no
"""

    rows = receivables_worker._project_authoritative_balance_rows_from_onec(
        onec_engine,
        operations_sql=cte_sql,
        snapshot_date=date(2026, 3, 20),
        opening_balance_date=date(2025, 1, 1),
        include_sql_opening=False,
    )

    by_ref = {row.counterparty_ref: row for row in rows}
    assert by_ref["cp-a"].current_balance == Decimal("80")
    assert by_ref["cp-a"].counterparty_name == "Контрагент A"
    assert by_ref["cp-a"].current_manager_ref == "mgr-2"
    assert by_ref["cp-a"].current_manager_name == "Менеджер 2"
    assert by_ref["cp-b"].current_balance == Decimal("40")
    assert by_ref["cp-c"].current_balance == Decimal("50")
    assert by_ref["cp-d"].current_balance == Decimal("70")


def test_run_receivable_ledger_sync_rebuilds_snapshot_from_ledger(
    monkeypatch,
) -> None:
    app_engine = create_engine("sqlite:///:memory:")
    onec_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(app_engine)
    _setup_layered_onec_source(onec_engine)

    monkeypatch.setattr(receivables_worker, "_get_onec_engine", lambda: onec_engine)
    monkeypatch.setattr(receivables_worker, "_get_app_engine", lambda: app_engine)
    monkeypatch.setattr(
        receivables_worker,
        "_resolve_employee_counterparty_refs",
        lambda *_args, **_kwargs: ("cp-b",),
    )
    monkeypatch.setattr(
        receivables_worker,
        "_resolve_staff_rows",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        receivables_worker, "_resolve_buyer_counterparty_refs", lambda *_args, **_kwargs: ()
    )

    result = run_receivable_ledger_sync(
        operations_sql=NORMALIZED_SQL,
        snapshot_date=date(2026, 3, 20),
        window_start=datetime(2026, 3, 20, 0, 0, 0),
        window_end=datetime(2026, 3, 21, 0, 0, 0),
        employee_counterparty_refs=("cp-b",),
    )

    assert result["balance_source_mode"] == "ledger_events_authoritative"
    assert result["snapshots"] >= 1
    with Session(app_engine) as session:
        snapshot = session.execute(
            select(ReceivableBalanceSnapshot).where(
                ReceivableBalanceSnapshot.snapshot_date == date(2026, 3, 20),
                ReceivableBalanceSnapshot.counterparty_ref == "cp-a",
            )
        ).scalar_one()
        assert snapshot.current_balance == Decimal("55.00")


def test_run_receivable_history_backfill_rebuilds_target_dates(monkeypatch) -> None:
    app_engine = create_engine("sqlite:///:memory:")
    onec_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(app_engine)
    _setup_layered_onec_source(onec_engine)
    monkeypatch.setattr(receivables_worker, "_get_app_engine", lambda: app_engine)
    monkeypatch.setattr(receivables_worker, "_get_onec_engine", lambda: onec_engine)
    monkeypatch.setattr(
        receivables_worker, "_resolve_buyer_counterparty_refs", lambda *_args, **_kwargs: ()
    )
    monkeypatch.setattr(
        receivables_worker,
        "build_receivable_layer_extractors",
        _build_fake_layer_extractors,
    )
    monkeypatch.setattr(
        "app.services.receivables_extractors.build_receivable_layer_extractors",
        _build_fake_layer_extractors,
    )
    monkeypatch.setattr(receivables_worker, "_resolve_staff_rows", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        "app.services.receivables._authoritative_layer_opening_dates",
        lambda *_args, **_kwargs: {
            "regular_opening": date(2026, 2, 28),
            "employee_opening": date(2026, 3, 19),
        },
    )

    result = run_receivable_history_backfill(
        date_from=date(2026, 3, 1),
        date_to=date(2026, 3, 20),
        opening_balance_date=date(2025, 1, 1),
        rebuild_snapshot_dates=(date(2026, 3, 20),),
        employee_counterparty_refs=("cp-b",),
        onec_engine=onec_engine,
        app_engine=app_engine,
    )

    assert result["opening"] is not None
    assert result["processed"] >= 8
    assert "2026-03-20" in result["rebuilds"]
    assert result["rebuilds"]["2026-03-20"]["snapshots"] >= 1

    with Session(app_engine) as session:
        snapshots = (
            session.execute(
                select(ReceivableBalanceSnapshot).where(
                    ReceivableBalanceSnapshot.snapshot_date == date(2026, 3, 20)
                )
            )
            .scalars()
            .all()
        )
        assert snapshots
        assert any(item.counterparty_ref == "cp-open-regular" for item in snapshots)
