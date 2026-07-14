from __future__ import annotations

import os
import tempfile
from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.dependencies import get_db
from app.core.config import get_settings
from app.main import app
from app.models import Base
from app.services.receivables import OneCReceivableLedgerExtractor, sync_receivable_ledger
from app.services.staffing import sync_staffing_data
from tests.test_receivables import NORMALIZED_SQL, _setup_onec_source
from tests.test_staffing import _fact_rows, _plan_rows, _staff_rows


def setup_db():
    fd, path = tempfile.mkstemp(prefix="management_tasks_api_", suffix=".db")
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
    events = extractor.fetch_receivable_events()

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
            snapshot_dates=[date(2026, 3, 20)],
        )
        session.commit()
    onec_engine.dispose()


def test_management_task_payloads_api(monkeypatch) -> None:
    engine, path = setup_db()
    seed_management_data(engine)

    monkeypatch.setenv("MANAGEMENT_INTERNAL_API_TOKEN", "secret-token")
    get_settings.cache_clear()
    app.dependency_overrides = {get_db: override_db(engine)}
    client = TestClient(app)

    unauthorized = client.get("/api/management/task-payloads", params={"date": "2026-03-20"})
    assert unauthorized.status_code == 401

    response = client.get(
        "/api/management/task-payloads",
        params={"date": "2026-03-20"},
        headers={"Authorization": "Bearer secret-token"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["freshness_status"] == "fresh"
    assert payload["payload"]
    rule_codes = {item["rule_code"] for item in payload["payload"]}
    assert "receivable_new_daily" not in rule_codes
    assert "staffing_shift_deficit" in rule_codes

    app.dependency_overrides = {}
    get_settings.cache_clear()
    engine.dispose()
    if os.path.exists(path):
        os.remove(path)
