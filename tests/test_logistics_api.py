from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_db, get_engine
from app.core.config import get_settings
from app.main import app
from app.models import (
    Base,
    LogisticsDriver,
    LogisticsTransfer,
    LogisticsTransferEvent,
    LogisticsTransferState,
    LogisticsUser,
    LogisticsWarehouse,
)


def setup_db():
    fd, path = tempfile.mkstemp(prefix="logistics_api_", suffix=".db")
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


def _auth_headers(token: str = "logistics-token") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _configure_logistics_auth(monkeypatch, token: str = "logistics-token") -> dict[str, str]:
    monkeypatch.setenv("LOGISTICS_INTERNAL_API_TOKEN", token)
    monkeypatch.delenv("MANAGEMENT_INTERNAL_API_TOKEN", raising=False)
    monkeypatch.delenv("RETURN_SCHEME_INTERNAL_API_TOKEN", raising=False)
    get_settings.cache_clear()
    get_engine.cache_clear()
    return _auth_headers(token)


def _id_maps(engine):
    with Session(engine) as session:
        users = {row.full_name: row.id for row in session.scalars(select(LogisticsUser)).all()}
        warehouses = {
            row.external_id: row.id for row in session.scalars(select(LogisticsWarehouse)).all()
        }
        drivers = {row.full_name: row.id for row in session.scalars(select(LogisticsDriver)).all()}
        transfer = session.scalar(select(LogisticsTransfer))
        return {
            "users": users,
            "warehouses": warehouses,
            "drivers": drivers,
            "transfer_id": transfer.id,
        }


def _seed_reference_data(client: TestClient, headers: dict[str, str]) -> None:
    warehouses = [
        {"external_id": "store-1", "name": "Магазин 1", "kind": "store"},
        {"external_id": "central", "name": "ЦС", "kind": "central"},
        {"external_id": "store-2", "name": "Магазин 2", "kind": "store"},
    ]
    assert (
        client.post("/api/logistics/sync/warehouses", json=warehouses, headers=headers).status_code
        == 200
    )

    drivers = [{"external_id": "driver-1", "full_name": "Иван Водитель"}]
    assert (
        client.post("/api/logistics/sync/drivers", json=drivers, headers=headers).status_code == 200
    )

    users = [
        {
            "external_id": "user-sender",
            "telegram_user_id": 101,
            "username": "sender_user",
            "full_name": "Отправитель",
            "role": "sender",
            "default_warehouse_external_id": "store-1",
        },
        {
            "external_id": "user-receiver",
            "telegram_user_id": 202,
            "username": "receiver_user",
            "full_name": "Получатель",
            "role": "receiver",
            "default_warehouse_external_id": "central",
        },
        {
            "external_id": "user-wrong-receiver",
            "telegram_user_id": 303,
            "username": "wrong_receiver",
            "full_name": "Неверная Точка",
            "role": "receiver",
            "default_warehouse_external_id": "store-2",
        },
        {
            "external_id": "user-logist",
            "telegram_user_id": 404,
            "username": "logist_user",
            "full_name": "Логист",
            "role": "logist",
            "default_warehouse_external_id": "central",
        },
    ]
    assert client.post("/api/logistics/sync/users", json=users, headers=headers).status_code == 200

    transfers = [
        {
            "external_id": "transfer-1",
            "document_number": "ПТ-000001",
            "document_date": "2026-03-28T10:00:00Z",
            "source_warehouse_external_id": "store-1",
            "target_warehouse_external_id": "central",
            "final_recipient_name": "ЦС",
            "barcode": "BC-0001",
            "status": "posted",
        }
    ]
    assert (
        client.post("/api/logistics/sync/transfers", json=transfers, headers=headers).status_code
        == 200
    )


def test_logistics_mvp_flow(monkeypatch) -> None:
    engine, path = setup_db()
    headers = _configure_logistics_auth(monkeypatch)
    app.dependency_overrides = {get_db: override_db(engine)}
    client = TestClient(app)

    assert client.get("/api/logistics/monitor").status_code == 401

    _seed_reference_data(client, headers)
    ids = _id_maps(engine)

    auth = client.post(
        "/api/logistics/auth/telegram",
        json={"telegram_user_id": 101, "username": "sender_user"},
        headers=headers,
    )
    assert auth.status_code == 200
    assert auth.json()["role"] == "sender"

    handoff_draft = client.post(
        "/api/logistics/handoffs/draft",
        json={
            "actor_user_id": ids["users"]["Отправитель"],
            "warehouse_id": ids["warehouses"]["store-1"],
            "driver_id": ids["drivers"]["Иван Водитель"],
            "default_dropoff_warehouse_id": ids["warehouses"]["central"],
            "comment": "Передача на ЦС",
        },
        headers=headers,
    )
    assert handoff_draft.status_code == 200
    draft_id = handoff_draft.json()["id"]

    duplicate_handoff = client.post(
        "/api/logistics/handoffs/draft",
        json={
            "actor_user_id": ids["users"]["Отправитель"],
            "warehouse_id": ids["warehouses"]["store-1"],
            "driver_id": ids["drivers"]["Иван Водитель"],
            "default_dropoff_warehouse_id": ids["warehouses"]["central"],
        },
        headers=headers,
    )
    assert duplicate_handoff.status_code == 409
    assert duplicate_handoff.json()["detail"]["draft_id"] == draft_id

    scanned = client.post(
        f"/api/logistics/handoffs/draft/{draft_id}/scan",
        json={
            "actor_user_id": ids["users"]["Отправитель"],
            "barcode": "BC-0001",
        },
        headers=headers,
    )
    assert scanned.status_code == 200
    assert scanned.json()["item_count"] == 1

    confirmed = client.post(
        f"/api/logistics/handoffs/draft/{draft_id}/confirm",
        json={
            "actor_user_id": ids["users"]["Отправитель"],
            "comment": "Передано водителю",
            "idempotency_key": "handoff-1",
            "photos": [{"telegram_file_id": "photo-handoff"}],
        },
        headers=headers,
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["processed_count"] == 1

    reconfirmed = client.post(
        f"/api/logistics/handoffs/draft/{draft_id}/confirm",
        json={
            "actor_user_id": ids["users"]["Отправитель"],
            "idempotency_key": "handoff-1",
        },
        headers=headers,
    )
    assert reconfirmed.status_code == 200

    expected = client.get(
        "/api/logistics/expected-deliveries",
        params={"warehouse_id": ids["warehouses"]["central"]},
        headers=headers,
    )
    assert expected.status_code == 200
    expected_items = expected.json()
    assert len(expected_items) == 1
    assert expected_items[0]["driver_name"] == "Иван Водитель"

    monitor = client.get("/api/logistics/monitor", params={"status": "in_transit"}, headers=headers)
    assert monitor.status_code == 200
    assert len(monitor.json()) == 1

    wrong_receipt = client.post(
        "/api/logistics/receipts/draft",
        json={
            "actor_user_id": ids["users"]["Неверная Точка"],
            "warehouse_id": ids["warehouses"]["store-2"],
        },
        headers=headers,
    )
    assert wrong_receipt.status_code == 200
    wrong_scan = client.post(
        f"/api/logistics/receipts/draft/{wrong_receipt.json()['id']}/scan",
        json={
            "actor_user_id": ids["users"]["Неверная Точка"],
            "barcode": "BC-0001",
        },
        headers=headers,
    )
    assert wrong_scan.status_code == 409
    assert "expected dropoff warehouse" in wrong_scan.json()["detail"]

    receipt_draft = client.post(
        "/api/logistics/receipts/draft",
        json={
            "actor_user_id": ids["users"]["Получатель"],
            "warehouse_id": ids["warehouses"]["central"],
        },
        headers=headers,
    )
    assert receipt_draft.status_code == 200
    receipt_id = receipt_draft.json()["id"]

    receipt_scan = client.post(
        f"/api/logistics/receipts/draft/{receipt_id}/scan",
        json={
            "actor_user_id": ids["users"]["Получатель"],
            "barcode": "BC-0001",
        },
        headers=headers,
    )
    assert receipt_scan.status_code == 200

    receipt_confirm = client.post(
        f"/api/logistics/receipts/draft/{receipt_id}/confirm",
        json={
            "actor_user_id": ids["users"]["Получатель"],
            "comment": "Принято на ЦС",
        },
        headers=headers,
    )
    assert receipt_confirm.status_code == 200

    expected_after = client.get(
        "/api/logistics/expected-deliveries",
        params={"warehouse_id": ids["warehouses"]["central"]},
        headers=headers,
    )
    assert expected_after.status_code == 200
    assert expected_after.json() == []

    incident = client.post(
        f"/api/logistics/transfers/{ids['transfer_id']}/incident",
        json={
            "actor_user_id": ids["users"]["Логист"],
            "warehouse_id": ids["warehouses"]["central"],
            "comment": "Проверка API",
            "idempotency_key": "incident-api-1",
            "photos": [{"telegram_file_id": "photo-api"}],
        },
        headers=headers,
    )
    assert incident.status_code == 200

    history = client.get(f"/api/logistics/transfers/{ids['transfer_id']}/history", headers=headers)
    assert history.status_code == 200
    events = history.json()
    assert events[0]["source"] == "api"
    assert events[0]["photos"][0]["telegram_file_id"] == "photo-api"
    assert [event["event_type"] for event in events[1:3]] == [
        "accepted_at_point",
        "handed_to_driver",
    ]
    assert events[2]["source"] == "telegram"
    assert events[2]["photos"][0]["telegram_file_id"] == "photo-handoff"

    duplicate_receipt = client.post(
        "/api/logistics/receipts/draft",
        json={
            "actor_user_id": ids["users"]["Получатель"],
            "warehouse_id": ids["warehouses"]["central"],
        },
        headers=headers,
    )
    duplicate_scan = client.post(
        f"/api/logistics/receipts/draft/{duplicate_receipt.json()['id']}/scan",
        json={
            "actor_user_id": ids["users"]["Получатель"],
            "barcode": "BC-0001",
        },
        headers=headers,
    )
    assert duplicate_scan.status_code == 409
    assert "already accepted" in duplicate_scan.json()["detail"]

    with Session(engine) as session:
        events_count = session.query(LogisticsTransferEvent).count()
        assert events_count == 3

    app.dependency_overrides = {}
    get_settings.cache_clear()
    get_engine.cache_clear()
    engine.dispose()
    if os.path.exists(path):
        os.remove(path)


def test_logistics_monitor_is_read_only_for_virtual_state(monkeypatch) -> None:
    engine, path = setup_db()
    headers = _configure_logistics_auth(monkeypatch)
    app.dependency_overrides = {get_db: override_db(engine)}
    client = TestClient(app)

    with Session(engine) as session:
        store = LogisticsWarehouse(external_id="store-1", name="Магазин 1", kind="store")
        central = LogisticsWarehouse(external_id="central", name="ЦС", kind="central")
        session.add_all([store, central])
        session.flush()
        session.add(
            LogisticsTransfer(
                external_id="transfer-virtual",
                document_number="ПТ-000777",
                document_date=datetime(2026, 3, 28, 10, 0, tzinfo=timezone.utc),
                source_warehouse_id=store.id,
                target_warehouse_id=central.id,
                final_recipient_name="ЦС",
                barcode="BC-VIRTUAL",
                onec_status="posted",
            )
        )
        session.commit()

    with Session(engine) as session:
        assert session.query(LogisticsTransferState).count() == 0

    monitor = client.get("/api/logistics/monitor", headers=headers)
    assert monitor.status_code == 200
    payload = monitor.json()
    assert len(payload) == 1
    assert payload[0]["status"] == "at_warehouse"
    assert payload[0]["current_warehouse_name"] == "Магазин 1"
    assert payload[0]["last_event_type"] == "synced"

    with Session(engine) as session:
        assert session.query(LogisticsTransferState).count() == 0

    app.dependency_overrides = {}
    get_settings.cache_clear()
    get_engine.cache_clear()
    engine.dispose()
    if os.path.exists(path):
        os.remove(path)


def test_get_engine_is_cached(monkeypatch) -> None:
    fd, path = tempfile.mkstemp(prefix="logistics_engine_", suffix=".db")
    os.close(fd)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    get_settings.cache_clear()
    get_engine.cache_clear()

    engine_1 = get_engine()
    engine_2 = get_engine()

    assert engine_1 is engine_2

    get_engine.cache_clear()
    get_settings.cache_clear()
    engine_1.dispose()
    if os.path.exists(path):
        os.remove(path)
