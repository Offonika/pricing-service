from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta
from decimal import Decimal
from time import perf_counter
from xml.etree import ElementTree

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.dependencies import get_db, get_engine
from app.core.config import get_settings
from app.main import app
from app.models import Base, LogisticsManualReview, SiteOrderExecutionCase
from app.services.site_order_state_projection import StateProjectionLookup, load_state_projection


def _setup_db():
    fd, path = tempfile.mkstemp(prefix="site_order_projection_", suffix=".db")
    os.close(fd)
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    return engine, path


def _override_db(engine):
    def override():
        with Session(engine) as session:
            yield session

    return override


def _seed_projection(engine) -> None:
    with Session(engine) as session:
        case = SiteOrderExecutionCase(
            site_order_number="245383",
            onec_order_external_id="РБГУ0063466",
            bitrix_deal_id=90383,
            current_derived_status="execution_assembled",
            current_crm_stage="FINAL_INVOICE",
            payment_status="paid",
            payload={
                "state_projection": {
                    "debt_amount": "0.00",
                    "site_status": "F",
                },
                "execution_reconciliation": {
                    "decision": {"action": "noop", "reason": "waiting"},
                    "snapshot": {"site_status": "F"},
                },
            },
            updated_at=datetime.now(),
        )
        session.add(case)
        session.flush()
        session.add(
            LogisticsManualReview(
                review_type="site_order_execution_conflict",
                source_document_type="site_order",
                source_external_id="245383",
                reason="issued_and_returned",
            )
        )
        session.commit()


def test_state_batch_reads_only_saved_projection_and_returns_xml(monkeypatch) -> None:
    engine, path = _setup_db()
    monkeypatch.setenv("LOGISTICS_INTERNAL_API_TOKEN", "projection-token")
    get_settings.cache_clear()
    get_engine.cache_clear()
    app.dependency_overrides = {get_db: _override_db(engine)}
    client = TestClient(app)
    try:
        _seed_projection(engine)
        response = client.post(
            "/api/logistics/site-orders/state-batch?format=xml",
            headers={"Authorization": "Bearer projection-token"},
            json={
                "orders": [
                    {"onec_order_number": "РБГУ0063466"},
                    {"site_order_number": "999999"},
                ]
            },
        )

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/xml")
        nodes = ElementTree.fromstring(response.content).findall("order")
        assert len(nodes) == 2
        first = {child.tag: child.text or "" for child in nodes[0]}
        assert first["site_order_number"] == "245383"
        assert first["crm_stage"] == "FINAL_INVOICE"
        assert first["logistics_state"] == "execution_assembled"
        assert first["payment_state"] == "paid"
        assert Decimal(first["debt_amount"]) == Decimal("0.00")
        assert first["site_status"] == "F"
        assert first["review_required"] == "true"
        assert first["review_reason"] == "issued_and_returned"
        second = {child.tag: child.text or "" for child in nodes[1]}
        assert second["site_order_number"] == "999999"
        assert second["stale"] == "true"
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()
        engine.dispose()
        os.remove(path)


def test_state_batch_requires_auth_and_rejects_duplicate_or_oversized_batches(
    monkeypatch,
) -> None:
    engine, path = _setup_db()
    monkeypatch.setenv("LOGISTICS_INTERNAL_API_TOKEN", "projection-token")
    get_settings.cache_clear()
    get_engine.cache_clear()
    app.dependency_overrides = {get_db: _override_db(engine)}
    client = TestClient(app)
    endpoint = "/api/logistics/site-orders/state-batch?format=xml"
    try:
        assert client.post(endpoint, json={"orders": []}).status_code == 401
        headers = {"Authorization": "Bearer projection-token"}
        duplicate = {"orders": [{"site_order_number": "245383"}] * 2}
        assert client.post(endpoint, headers=headers, json=duplicate).status_code == 422
        oversized = {"orders": [{"site_order_number": str(240000 + index)} for index in range(501)]}
        assert client.post(endpoint, headers=headers, json=oversized).status_code == 422
        empty = client.post(endpoint, headers=headers, json={"orders": []})
        assert empty.status_code == 200
        assert ElementTree.fromstring(empty.content).findall("order") == []
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()
        engine.dispose()
        os.remove(path)


def test_projection_marks_old_snapshot_stale_and_identifier_conflict_for_review() -> None:
    engine, path = _setup_db()
    try:
        with Session(engine) as session:
            now = datetime(2026, 9, 4, 12, 0)
            session.add_all(
                [
                    SiteOrderExecutionCase(
                        site_order_number="245001",
                        onec_order_external_id="РБГУ0000001",
                        current_derived_status="execution_waiting",
                        current_crm_stage="EXECUTING",
                        updated_at=now - timedelta(hours=1),
                    ),
                    SiteOrderExecutionCase(
                        site_order_number="245002",
                        onec_order_external_id="РБГУ0000001",
                        current_derived_status="execution_waiting",
                        current_crm_stage="EXECUTING",
                        updated_at=now,
                    ),
                ]
            )
            session.commit()
            rows = load_state_projection(
                session,
                [StateProjectionLookup("РБГУ0000001", None)],
                stale_after_seconds=900,
                now=now,
            )

        assert len(rows) == 1
        assert rows[0].review_required is True
        assert rows[0].review_reason == "order_identifiers_conflict"
        assert rows[0].stale is False
    finally:
        engine.dispose()
        os.remove(path)


@pytest.mark.parametrize("batch_size", [1, 100, 500])
def test_state_batch_handles_contract_batch_sizes_within_two_seconds(
    monkeypatch,
    batch_size: int,
) -> None:
    engine, path = _setup_db()
    monkeypatch.setenv("LOGISTICS_INTERNAL_API_TOKEN", "projection-token")
    get_settings.cache_clear()
    get_engine.cache_clear()
    app.dependency_overrides = {get_db: _override_db(engine)}
    client = TestClient(app)
    try:
        started_at = perf_counter()
        response = client.post(
            "/api/logistics/site-orders/state-batch?format=xml",
            headers={"Authorization": "Bearer projection-token"},
            json={
                "orders": [
                    {"site_order_number": str(300000 + index)} for index in range(batch_size)
                ]
            },
        )
        elapsed = perf_counter() - started_at

        assert response.status_code == 200
        assert len(ElementTree.fromstring(response.content).findall("order")) == batch_size
        assert elapsed < 2.0
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()
        engine.dispose()
        os.remove(path)
