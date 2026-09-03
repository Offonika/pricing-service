from __future__ import annotations

from collections.abc import Generator

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.dependencies import get_db
from app.core.config import get_settings
from app.main import app
from app.models.order_assembly_queue import OrderAssemblyCrmOutbox


def _override_db(engine):
    def override() -> Generator[Session, None, None]:
        with Session(engine) as session:
            yield session

    return override


def _configure(monkeypatch) -> None:
    monkeypatch.setenv("ORDER_FULFILLMENT_INTERNAL_API_TOKEN", "order-token")
    monkeypatch.delenv("LOGISTICS_INTERNAL_API_TOKEN", raising=False)
    monkeypatch.delenv("MANAGEMENT_INTERNAL_API_TOKEN", raising=False)
    get_settings.cache_clear()


def _form(**overrides) -> dict[str, str]:
    values = {
        "event_key": "assembled-order:order-1:20260903120000",
        "order": "218530",
        "status": "assembled",
        "assembly_source": "customer_order",
        "assembly_ref": "order-1",
        "assembled_at": "2026-09-03 12:00:00+00:00",
        "execution_status": "06",
        "delivery_code": "MM_COURIER",
        "payment_mode": "by_agreement",
        "onec_order_number": "РБГУ0033819",
    }
    values.update(overrides)
    return values


def test_assembly_event_endpoint_is_idempotent(monkeypatch) -> None:
    _configure(monkeypatch)
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    OrderAssemblyCrmOutbox.__table__.create(engine)
    app.dependency_overrides = {get_db: _override_db(engine)}
    client = TestClient(app)
    try:
        first = client.post(
            "/api/order-fulfillment/assembly-events",
            data=_form(),
            headers={"Authorization": "Bearer order-token"},
        )
        second = client.post(
            "/api/order-fulfillment/assembly-events",
            data=_form(),
            headers={"Authorization": "Bearer order-token"},
        )
    finally:
        app.dependency_overrides = {}

    assert first.status_code == 200
    assert first.json()["duplicate"] is False
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert second.json()["outbox_id"] == first.json()["outbox_id"]


def test_assembly_event_endpoint_rejects_invalid_mm_courier_status(monkeypatch) -> None:
    _configure(monkeypatch)
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    OrderAssemblyCrmOutbox.__table__.create(engine)
    app.dependency_overrides = {get_db: _override_db(engine)}
    client = TestClient(app)
    try:
        response = client.post(
            "/api/order-fulfillment/assembly-events",
            data=_form(execution_status="05"),
            headers={"Authorization": "Bearer order-token"},
        )
    finally:
        app.dependency_overrides = {}

    assert response.status_code == 422
    assert "requires execution_status=06" in response.json()["detail"]
