from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_db, get_engine
from app.core.config import get_settings
from app.main import app
from app.models import (
    CustomerReturnAction,
    CustomerReturnEvent,
    CustomerReturnShipment,
)


def _setup_db():
    fd, path = tempfile.mkstemp(prefix="customer_returns_api_", suffix=".db")
    os.close(fd)
    engine = create_engine(f"sqlite:///{path}")
    CustomerReturnShipment.__table__.create(engine)
    CustomerReturnEvent.__table__.create(engine)
    CustomerReturnAction.__table__.create(engine)
    return engine, path


def _override_db(engine):
    def override():
        db = Session(engine)
        try:
            yield db
        finally:
            db.close()

    return override


def _configure_auth(monkeypatch, token: str = "returns-token") -> dict[str, str]:
    monkeypatch.setenv("LOGISTICS_INTERNAL_API_TOKEN", token)
    get_settings.cache_clear()
    get_engine.cache_clear()
    return {"Authorization": f"Bearer {token}"}


def test_customer_return_end_to_end_and_deduplication(monkeypatch) -> None:
    engine, path = _setup_db()
    headers = _configure_auth(monkeypatch)
    app.dependency_overrides = {get_db: _override_db(engine)}
    client = TestClient(app)

    try:
        assert client.get("/api/customer-returns").status_code == 401

        create_payload = {
            "carrier": "russian_post",
            "tracking_number": "1234 5678 9012 34",
            "source": "bitrix24",
            "source_ref": "task-3507:return-1",
            "bitrix_case_id": "3507-1",
            "onec_order_ref": "ORDER-101",
            "created_by_bitrix_user_id": "6357",
        }
        created = client.post("/api/customer-returns", json=create_payload, headers=headers)
        assert created.status_code == 200
        assert created.json()["created"] is True
        shipment = created.json()["shipment"]
        shipment_id = shipment["id"]
        assert shipment["tracking_number"] == "12345678901234"
        assert shipment["status"] == "registered"
        assert [event["event_type"] for event in shipment["events"]] == ["registered"]

        duplicate = client.post(
            "/api/customer-returns",
            json={**create_payload, "source_ref": "a-second-reference"},
            headers=headers,
        )
        assert duplicate.status_code == 200
        assert duplicate.json()["created"] is False
        assert duplicate.json()["shipment"]["id"] == shipment_id

        source_conflict = client.post(
            "/api/customer-returns",
            json={
                "carrier": "cdek",
                "tracking_number": "CDEK-12345",
                "source_ref": "task-3507:return-1",
            },
            headers=headers,
        )
        assert source_conflict.status_code == 409

        now = datetime.now(timezone.utc)
        deadline = now + timedelta(days=5)
        arrival_payload = {
            "status_code": "arrived-at-post-office",
            "status_text": "Прибыло в место вручения",
            "occurred_at": now.isoformat(),
            "external_event_id": "post-event-101",
            "storage_deadline_at": deadline.isoformat(),
            "payload": {"operation": "arrival"},
        }
        arrived = client.post(
            f"/api/customer-returns/{shipment_id}/carrier-events",
            json=arrival_payload,
            headers=headers,
        )
        assert arrived.status_code == 200
        assert arrived.json()["event_created"] is True
        arrived_shipment = arrived.json()["shipment"]
        assert arrived_shipment["status"] == "arrived_at_pickup_point"
        assert {action["action_type"] for action in arrived_shipment["actions"]} == {
            "arrival_task",
            "storage_reminder_3d",
            "storage_reminder_1d",
        }

        duplicate_event = client.post(
            f"/api/customer-returns/{shipment_id}/carrier-events",
            json=arrival_payload,
            headers=headers,
        )
        assert duplicate_event.status_code == 200
        assert duplicate_event.json()["event_created"] is False
        assert len(duplicate_event.json()["shipment"]["events"]) == 2
        assert len(duplicate_event.json()["shipment"]["actions"]) == 3

        due = client.get(
            "/api/customer-returns/actions/due",
            params={"as_of": (now + timedelta(minutes=2)).isoformat()},
            headers=headers,
        )
        assert due.status_code == 200
        assert [action["action_type"] for action in due.json()] == ["arrival_task"]
        arrival_action_id = due.json()[0]["id"]

        completed = client.post(
            f"/api/customer-returns/actions/{arrival_action_id}/complete",
            json={"external_reference": "BITRIX-TASK-9001"},
            headers=headers,
        )
        assert completed.status_code == 200
        assert completed.json()["status"] == "completed"
        completed_again = client.post(
            f"/api/customer-returns/actions/{arrival_action_id}/complete",
            json={"external_reference": "BITRIX-TASK-9001"},
            headers=headers,
        )
        assert completed_again.status_code == 200
        conflicting_completion = client.post(
            f"/api/customer-returns/actions/{arrival_action_id}/complete",
            json={"external_reference": "BITRIX-TASK-OTHER"},
            headers=headers,
        )
        assert conflicting_completion.status_code == 409

        pickup_payload = {
            "actor_bitrix_user_id": "6357",
            "occurred_at": (now + timedelta(hours=1)).isoformat(),
            "idempotency_key": "pickup-click-1",
            "comment": "Забрал Андрей Платонов",
        }
        picked_up = client.post(
            f"/api/customer-returns/{shipment_id}/pickup",
            json=pickup_payload,
            headers=headers,
        )
        assert picked_up.status_code == 200
        assert picked_up.json()["status"] == "picked_up"
        assert picked_up.json()["picked_up_by_bitrix_user_id"] == "6357"
        assert (
            sum(
                action["action_type"] == "onec_return_control"
                for action in picked_up.json()["actions"]
            )
            == 1
        )

        picked_up_again = client.post(
            f"/api/customer-returns/{shipment_id}/pickup",
            json={**pickup_payload, "idempotency_key": "second-click"},
            headers=headers,
        )
        assert picked_up_again.status_code == 200
        assert (
            sum(
                event["event_type"] == "pickup_confirmed"
                for event in picked_up_again.json()["events"]
            )
            == 1
        )

        late_transport_event = client.post(
            f"/api/customer-returns/{shipment_id}/carrier-events",
            json={
                "status_code": "in_transit",
                "occurred_at": (now - timedelta(days=1)).isoformat(),
                "external_event_id": "late-post-event",
            },
            headers=headers,
        )
        assert late_transport_event.status_code == 200
        assert late_transport_event.json()["shipment"]["status"] == "picked_up"

        onec = client.post(
            f"/api/customer-returns/{shipment_id}/onec-confirmation",
            json={
                "onec_return_ref": "0xRETURN101",
                "occurred_at": (now + timedelta(hours=2)).isoformat(),
            },
            headers=headers,
        )
        assert onec.status_code == 200
        assert onec.json()["status"] == "onec_return_confirmed"
        assert onec.json()["onec_return_ref"] == "0xRETURN101"
        assert (
            next(
                action
                for action in onec.json()["actions"]
                if action["action_type"] == "onec_return_control"
            )["status"]
            == "skipped"
        )

        conflicting_onec = client.post(
            f"/api/customer-returns/{shipment_id}/onec-confirmation",
            json={"onec_return_ref": "0xOTHER"},
            headers=headers,
        )
        assert conflicting_onec.status_code == 409

        listed = client.get(
            "/api/customer-returns",
            params={"carrier": "russian_post", "status": "onec_return_confirmed"},
            headers=headers,
        )
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()] == [shipment_id]

        with Session(engine) as session:
            assert len(session.scalars(select(CustomerReturnShipment)).all()) == 1
            assert len(session.scalars(select(CustomerReturnEvent)).all()) == 5
            assert len(session.scalars(select(CustomerReturnAction)).all()) == 5
    finally:
        app.dependency_overrides = {}
        get_settings.cache_clear()
        get_engine.cache_clear()
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_unknown_carrier_status_requires_manual_review(monkeypatch) -> None:
    engine, path = _setup_db()
    headers = _configure_auth(monkeypatch)
    app.dependency_overrides = {get_db: _override_db(engine)}
    client = TestClient(app)

    try:
        created = client.post(
            "/api/customer-returns",
            json={"carrier": "cdek", "tracking_number": "CDEK-998877"},
            headers=headers,
        )
        shipment_id = created.json()["shipment"]["id"]
        event = client.post(
            f"/api/customer-returns/{shipment_id}/carrier-events",
            json={"status_code": "provider_new_status", "external_event_id": "new-1"},
            headers=headers,
        )

        assert event.status_code == 200
        assert event.json()["shipment"]["status"] == "exception"
        assert (
            event.json()["shipment"]["events"][-1]["carrier_status_code"] == "provider_new_status"
        )
    finally:
        app.dependency_overrides = {}
        get_settings.cache_clear()
        get_engine.cache_clear()
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)
