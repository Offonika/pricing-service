from __future__ import annotations

import os
import tempfile
from datetime import date, datetime
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.dependencies import get_db
from app.core.config import get_settings
from app.main import app
from app.models import Base
from app.models.receivable_balance_snapshot import ReceivableBalanceSnapshot
from app.services.receivables import (
    OneCReceivableLedgerExtractor,
    ReceivableLedgerRow,
    sync_receivable_ledger,
)
from app.services.staffing import sync_staffing_data
from tests.test_receivables import NORMALIZED_SQL, _setup_onec_source
from tests.test_staffing import _fact_rows, _plan_rows, _staff_rows


def setup_db():
    fd, path = tempfile.mkstemp(prefix="management_health_api_", suffix=".db")
    os.close(fd)
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    return engine, path


def override_db(engine):
    def _override():
        db = Session(engine)
        try:
            yield db
        finally:
            db.close()

    return _override


def seed_management_data(engine) -> None:
    onec_engine = create_engine("sqlite:///:memory:")
    _setup_onec_source(onec_engine)
    extractor = OneCReceivableLedgerExtractor(onec_engine, operations_sql=NORMALIZED_SQL)
    events = [
        ReceivableLedgerRow(
            source="onec_opening_import",
            event_type="opening_balance",
            external_document_ref="opening-2025-01-01-cp-a",
            external_document_number="opening-2025-01-01",
            external_document_date=datetime(2025, 1, 1),
            counterparty_ref="cp-a",
            counterparty_name="Контрагент A",
            contract_ref=None,
            contract_name=None,
            contract_kind_ref=None,
            contract_kind_name=None,
            manager_ref=None,
            manager_name=None,
            store_ref=None,
            store_name=None,
            source_layer="opening_import_1c",
            planned_payment_date=None,
            credit_depth_days=None,
            shipment_ban=None,
            line_no=1,
            amount_delta=Decimal("1.00"),
        ),
        *extractor.fetch_receivable_events(),
    ]

    with Session(engine) as session:
        sync_receivable_ledger(
            session,
            events,
            snapshot_date=date(2026, 3, 20),
            employee_counterparty_refs=["cp-b"],
            fired_manager_refs=["mgr-4"],
        )
        sync_staffing_data(
            session,
            staff_members=_staff_rows(),
            shift_plans=_plan_rows(),
            shift_facts=_fact_rows(),
            snapshot_dates=[date(2026, 3, 20), date(2026, 3, 21), date(2026, 3, 22)],
        )
        session.commit()
    onec_engine.dispose()


def test_management_health_api_reports_fresh_components(monkeypatch) -> None:
    engine, path = setup_db()
    seed_management_data(engine)

    monkeypatch.setenv("MANAGEMENT_INTERNAL_API_TOKEN", "secret-token")
    get_settings.cache_clear()
    app.dependency_overrides = {get_db: override_db(engine)}
    client = TestClient(app)

    response = client.get(
        "/api/management/health",
        params={"date": "2026-03-20"},
        headers={"Authorization": "Bearer secret-token"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["freshness_status"] == "fresh"
    components = {item["component"]: item for item in payload["components"]}
    assert components["receivables"]["freshness_status"] == "fresh"
    assert components["receivables"]["metrics"]["snapshot_reconciliation_match"] is True
    assert components["receivables"]["metrics"]["synthetic_ref_count"] == 0
    assert components["staffing"]["freshness_status"] == "fresh"
    assert components["task_payloads"]["freshness_status"] == "fresh"

    app.dependency_overrides = {}
    get_settings.cache_clear()
    engine.dispose()
    if os.path.exists(path):
        os.remove(path)


def test_management_health_api_degrades_synthetic_receivable_refs(monkeypatch) -> None:
    engine, path = setup_db()
    seed_management_data(engine)
    with Session(engine) as session:
        session.add(
            ReceivableBalanceSnapshot(
                snapshot_date=date(2026, 3, 20),
                counterparty_ref="synthetic:test",
                counterparty_name="Synthetic",
                current_balance=Decimal("1.00"),
                aged_bucket="0-7",
                activity_segment="active",
                payment_term_source="missing",
                is_overdue=False,
                shipment_ban=False,
            )
        )
        session.commit()

    monkeypatch.setenv("MANAGEMENT_INTERNAL_API_TOKEN", "secret-token")
    get_settings.cache_clear()
    app.dependency_overrides = {get_db: override_db(engine)}
    client = TestClient(app)

    response = client.get(
        "/api/management/health",
        params={"date": "2026-03-20"},
        headers={"Authorization": "Bearer secret-token"},
    )
    assert response.status_code == 200
    payload = response.json()
    components = {item["component"]: item for item in payload["components"]}
    receivables = components["receivables"]
    assert payload["status"] == "degraded"
    assert receivables["source_status"] == "partial"
    assert receivables["metrics"]["synthetic_ref_count"] == 1
    assert "synthetic_counterparty_refs" in receivables["metrics"]["quality_issues"]

    app.dependency_overrides = {}
    get_settings.cache_clear()
    engine.dispose()
    if os.path.exists(path):
        os.remove(path)


def test_management_health_api_uses_requested_receivable_date_not_future_latest(
    monkeypatch,
) -> None:
    engine, path = setup_db()
    seed_management_data(engine)
    with Session(engine) as session:
        sync_receivable_ledger(
            session,
            [],
            snapshot_date=date(2026, 3, 21),
            authoritative_balance_rows=[],
            employee_counterparty_refs=[],
            rebuild_read_models=True,
        )
        session.commit()

    monkeypatch.setenv("MANAGEMENT_INTERNAL_API_TOKEN", "secret-token")
    get_settings.cache_clear()
    app.dependency_overrides = {get_db: override_db(engine)}
    client = TestClient(app)

    response = client.get(
        "/api/management/health",
        params={"date": "2026-03-20"},
        headers={"Authorization": "Bearer secret-token"},
    )
    assert response.status_code == 200
    payload = response.json()
    components = {item["component"]: item for item in payload["components"]}
    receivables = components["receivables"]
    assert receivables["latest_snapshot_date"] == "2026-03-20"
    assert receivables["metrics"]["latest_balance_snapshot_date"] == "2026-03-20"
    assert receivables["metrics"]["latest_case_date"] == "2026-03-20"
    assert receivables["metrics"]["snapshot_reconciliation_match"] is True
    assert receivables["source_status"] == "ready"

    app.dependency_overrides = {}
    get_settings.cache_clear()
    engine.dispose()
    if os.path.exists(path):
        os.remove(path)


def test_management_health_api_reports_stale_receivables(monkeypatch) -> None:
    engine, path = setup_db()
    seed_management_data(engine)

    monkeypatch.setenv("MANAGEMENT_INTERNAL_API_TOKEN", "secret-token")
    get_settings.cache_clear()
    app.dependency_overrides = {get_db: override_db(engine)}
    client = TestClient(app)

    response = client.get(
        "/api/management/health",
        params={"date": "2026-03-22"},
        headers={"Authorization": "Bearer secret-token"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "degraded"
    assert payload["freshness_status"] == "stale"
    components = {item["component"]: item for item in payload["components"]}
    assert components["receivables"]["freshness_status"] == "stale"
    assert components["receivables"]["lag_days"] == 2
    assert components["staffing"]["freshness_status"] == "fresh"
    assert components["task_payloads"]["freshness_status"] == "stale"

    app.dependency_overrides = {}
    get_settings.cache_clear()
    engine.dispose()
    if os.path.exists(path):
        os.remove(path)
