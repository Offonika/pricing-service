from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.api.dependencies import get_db, get_engine
from app.core.config import get_settings
from app.main import app
from app.models import (
    Base,
    LogisticsDriver,
    LogisticsManualReview,
    LogisticsTransfer,
    LogisticsTransferEvent,
    LogisticsTransferState,
    LogisticsUser,
    LogisticsWarehouse,
    SiteOrderExecutionCase,
    SiteOrderExecutionEvent,
    SiteOrderStageOutbox,
)
from app.services import logistics


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


def test_logistics_state_rejects_stale_version_update() -> None:
    engine, path = setup_db()
    try:
        with Session(engine) as session:
            warehouse = LogisticsWarehouse(
                external_id="version-warehouse",
                name="Склад",
                kind="store",
            )
            session.add(warehouse)
            session.flush()
            transfer = LogisticsTransfer(
                external_id="version-transfer",
                document_number="РТУ-VERSION",
                document_date=datetime(2026, 8, 28, 9, 0),
                source_warehouse_id=warehouse.id,
                target_warehouse_id=warehouse.id,
                barcode="BC-VERSION",
            )
            session.add(transfer)
            session.flush()
            session.add(
                LogisticsTransferState(
                    transfer_id=transfer.id,
                    status="at_warehouse",
                    current_warehouse_id=warehouse.id,
                    last_event_type="synced",
                    last_event_at=datetime(2026, 8, 28, 9, 0),
                    version=1,
                )
            )
            session.commit()
            transfer_id = transfer.id

        with Session(engine) as first, Session(engine) as stale:
            first_state = first.get(LogisticsTransferState, transfer_id)
            stale_state = stale.get(LogisticsTransferState, transfer_id)
            assert first_state is not None
            assert stale_state is not None

            first_state.last_document_ref = "first"
            first.commit()
            assert first_state.version == 2

            stale_state.last_document_ref = "stale"
            with pytest.raises(StaleDataError):
                stale.commit()
    finally:
        engine.dispose()
        os.remove(path)


def test_logistics_mvp_flow(monkeypatch) -> None:
    engine, path = setup_db()
    headers = _configure_logistics_auth(monkeypatch)
    app.dependency_overrides = {get_db: override_db(engine)}
    client = TestClient(app)

    assert client.get("/api/logistics/monitor").status_code == 401

    _seed_reference_data(client, headers)
    ids = _id_maps(engine)
    forbidden_logist_handoff = client.post(
        "/api/logistics/handoffs/draft",
        json={
            "actor_user_id": ids["users"]["Логист"],
            "warehouse_id": ids["warehouses"]["central"],
            "driver_id": ids["drivers"]["Иван Водитель"],
            "default_dropoff_warehouse_id": ids["warehouses"]["store-1"],
        },
        headers=headers,
    )
    assert forbidden_logist_handoff.status_code == 403
    forbidden_logist_receipt = client.post(
        "/api/logistics/receipts/draft",
        json={
            "actor_user_id": ids["users"]["Логист"],
            "warehouse_id": ids["warehouses"]["central"],
        },
        headers=headers,
    )
    assert forbidden_logist_receipt.status_code == 403

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
            "default_dropoff_warehouse_id": ids["warehouses"]["store-2"],
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

    long_unknown_code = "X" * 255
    unknown_scan = client.post(
        f"/api/logistics/handoffs/draft/{draft_id}/scan",
        json={
            "actor_user_id": ids["users"]["Отправитель"],
            "lookup_code": long_unknown_code,
        },
        headers=headers,
    )
    assert unknown_scan.status_code == 404
    repeated_unknown_scan = client.post(
        f"/api/logistics/handoffs/draft/{draft_id}/scan",
        json={
            "actor_user_id": ids["users"]["Отправитель"],
            "lookup_code": long_unknown_code,
        },
        headers=headers,
    )
    assert repeated_unknown_scan.status_code == 404
    oversized_scan = client.post(
        f"/api/logistics/handoffs/draft/{draft_id}/scan",
        json={
            "actor_user_id": ids["users"]["Отправитель"],
            "lookup_code": "X" * 256,
        },
        headers=headers,
    )
    assert oversized_scan.status_code == 422

    scanned = client.post(
        f"/api/logistics/handoffs/draft/{draft_id}/scan",
        json={
            "actor_user_id": ids["users"]["Отправитель"],
            "barcode": "BC-0001",
            "dropoff_warehouse_id": ids["warehouses"]["store-2"],
        },
        headers=headers,
    )
    assert scanned.status_code == 200
    assert scanned.json()["item_count"] == 1
    assert scanned.json()["items"][0]["dropoff_warehouse_id"] == ids["warehouses"]["central"]
    assert scanned.json()["items"][0]["dropoff_warehouse_name"] == "ЦС"

    repeated_scan = client.post(
        f"/api/logistics/handoffs/draft/{draft_id}/scan",
        json={
            "actor_user_id": ids["users"]["Отправитель"],
            "barcode": "BC-0001",
        },
        headers=headers,
    )
    assert repeated_scan.status_code == 200
    assert repeated_scan.json()["item_count"] == 1

    long_idempotency_key = "handoff-" + "x" * 247
    confirmed = client.post(
        f"/api/logistics/handoffs/draft/{draft_id}/confirm",
        json={
            "actor_user_id": ids["users"]["Отправитель"],
            "comment": "Передано водителю",
            "idempotency_key": long_idempotency_key,
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
            "idempotency_key": long_idempotency_key,
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
    assert events[2]["source"] == "api"
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
    assert duplicate_scan.json()["detail"] == "Документ уже принят в этом магазине"

    with Session(engine) as session:
        events_count = session.query(LogisticsTransferEvent).count()
        assert events_count == 3
        handoff_event = session.scalar(
            select(LogisticsTransferEvent).where(
                LogisticsTransferEvent.event_type == "handed_to_driver"
            )
        )
        assert handoff_event is not None
        assert len(handoff_event.idempotency_key or "") <= 255
        unknown_review = session.scalar(
            select(LogisticsManualReview).where(LogisticsManualReview.review_type == "unknown_qr")
        )
        assert unknown_review is not None
        assert unknown_review.source_external_id == long_unknown_code[:64]
        assert unknown_review.payload["lookup_code"] == long_unknown_code
        assert unknown_review.payload["attempt_count"] == 2
        assert (
            session.query(LogisticsManualReview)
            .filter_by(review_type="unknown_qr", status="open")
            .count()
            == 1
        )

    app.dependency_overrides = {}
    get_settings.cache_clear()
    get_engine.cache_clear()
    engine.dispose()
    if os.path.exists(path):
        os.remove(path)


def test_handoff_destination_comes_from_document_without_draft_default(monkeypatch) -> None:
    engine, path = setup_db()
    headers = _configure_logistics_auth(monkeypatch)
    app.dependency_overrides = {get_db: override_db(engine)}
    client = TestClient(app)

    _seed_reference_data(client, headers)
    ids = _id_maps(engine)
    with Session(engine) as session:
        transfer = session.get(LogisticsTransfer, ids["transfer_id"])
        assert transfer is not None
        transfer.document_target_warehouse_id = ids["warehouses"]["store-2"]
        session.commit()

    draft = client.post(
        "/api/logistics/handoffs/draft",
        json={
            "actor_user_id": ids["users"]["Отправитель"],
            "warehouse_id": ids["warehouses"]["store-1"],
            "driver_id": ids["drivers"]["Иван Водитель"],
        },
        headers=headers,
    )
    assert draft.status_code == 200
    assert draft.json()["default_dropoff_warehouse_id"] is None

    scanned = client.post(
        f"/api/logistics/handoffs/draft/{draft.json()['id']}/scan",
        json={
            "actor_user_id": ids["users"]["Отправитель"],
            "barcode": "BC-0001",
            "dropoff_warehouse_id": ids["warehouses"]["central"],
        },
        headers=headers,
    )
    assert scanned.status_code == 200
    assert scanned.json()["items"][0]["dropoff_warehouse_id"] == ids["warehouses"]["store-2"]
    assert scanned.json()["items"][0]["dropoff_warehouse_name"] == "Магазин 2"

    app.dependency_overrides = {}
    get_settings.cache_clear()
    get_engine.cache_clear()
    engine.dispose()
    if os.path.exists(path):
        os.remove(path)


def test_handoff_unavailable_document_destination_creates_one_manual_review(monkeypatch) -> None:
    engine, path = setup_db()
    headers = _configure_logistics_auth(monkeypatch)
    app.dependency_overrides = {get_db: override_db(engine)}
    client = TestClient(app)

    _seed_reference_data(client, headers)
    ids = _id_maps(engine)
    with Session(engine) as session:
        destination = session.get(LogisticsWarehouse, ids["warehouses"]["central"])
        assert destination is not None
        destination.is_active = False
        session.commit()

    draft = client.post(
        "/api/logistics/handoffs/draft",
        json={
            "actor_user_id": ids["users"]["Отправитель"],
            "warehouse_id": ids["warehouses"]["store-1"],
            "driver_id": ids["drivers"]["Иван Водитель"],
        },
        headers=headers,
    )
    assert draft.status_code == 200

    for _ in range(2):
        scanned = client.post(
            f"/api/logistics/handoffs/draft/{draft.json()['id']}/scan",
            json={
                "actor_user_id": ids["users"]["Отправитель"],
                "barcode": "BC-0001",
            },
            headers=headers,
        )
        assert scanned.status_code == 409
        assert scanned.json()["detail"] == "handoff destination requires manual review"

    with Session(engine) as session:
        reviews = session.scalars(
            select(LogisticsManualReview).where(
                LogisticsManualReview.review_type == "handoff_destination_unresolved"
            )
        ).all()
        assert len(reviews) == 1
        assert reviews[0].transfer_id == ids["transfer_id"]

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


def test_logistics_route_run_lookup_and_external_carrier(monkeypatch) -> None:
    engine, path = setup_db()
    headers = _configure_logistics_auth(monkeypatch)
    app.dependency_overrides = {get_db: override_db(engine)}
    client = TestClient(app)

    _seed_reference_data(client, headers)
    ids = _id_maps(engine)

    lookup = client.get(
        "/api/logistics/units/lookup",
        params={"code": "BC-0001"},
        headers=headers,
    )
    assert lookup.status_code == 200
    assert lookup.json()["source_document_type"] == "transfer"

    route = client.post(
        "/api/logistics/route-runs",
        json={
            "external_id": "route-1",
            "route_name": "Утренний рейс",
            "driver_id": ids["drivers"]["Иван Водитель"],
            "items": [
                {
                    "lookup_code": "BC-0001",
                    "dropoff_warehouse_id": ids["warehouses"]["central"],
                    "leg_sequence": 1,
                }
            ],
        },
        headers=headers,
    )
    assert route.status_code == 200
    route_id = route.json()["id"]
    assert route.json()["items"][0]["status"] == "planned"

    handoff_draft = client.post(
        "/api/logistics/handoffs/draft",
        json={
            "actor_user_id": ids["users"]["Отправитель"],
            "warehouse_id": ids["warehouses"]["store-1"],
            "driver_id": ids["drivers"]["Иван Водитель"],
            "route_run_id": route_id,
            "default_dropoff_warehouse_id": ids["warehouses"]["central"],
        },
        headers=headers,
    )
    assert handoff_draft.status_code == 200
    draft_id = handoff_draft.json()["id"]
    scan = client.post(
        f"/api/logistics/handoffs/draft/{draft_id}/scan",
        json={"actor_user_id": ids["users"]["Отправитель"], "lookup_code": "BC-0001"},
        headers=headers,
    )
    assert scan.status_code == 200
    confirm = client.post(
        f"/api/logistics/handoffs/draft/{draft_id}/confirm",
        json={"actor_user_id": ids["users"]["Отправитель"]},
        headers=headers,
    )
    assert confirm.status_code == 200

    route_after_handoff = client.get("/api/logistics/route-runs", headers=headers)
    assert route_after_handoff.status_code == 200
    assert route_after_handoff.json()[0]["items"][0]["status"] == "in_transit"

    external_handoff = client.post(
        f"/api/logistics/transfers/{ids['transfer_id']}/external-carrier/handoff",
        json={
            "actor_user_id": ids["users"]["Логист"],
            "carrier_name": "СДЭК",
            "tracking_number": "CDEK-1",
        },
        headers=headers,
    )
    assert external_handoff.status_code == 200

    monitor = client.get(
        "/api/logistics/monitor",
        params={"with_external_carrier": True},
        headers=headers,
    )
    assert monitor.status_code == 200
    assert monitor.json()[0]["status"] == "with_external_carrier"
    assert monitor.json()[0]["route_run_id"] == route_id

    external_accept = client.post(
        f"/api/logistics/transfers/{ids['transfer_id']}/external-carrier/accept",
        json={
            "actor_user_id": ids["users"]["Логист"],
            "warehouse_id": ids["warehouses"]["central"],
        },
        headers=headers,
    )
    assert external_accept.status_code == 200

    route_after_accept = client.get("/api/logistics/route-runs", headers=headers)
    assert route_after_accept.json()[0]["items"][0]["status"] == "completed"

    app.dependency_overrides = {}
    get_settings.cache_clear()
    get_engine.cache_clear()
    engine.dispose()
    if os.path.exists(path):
        os.remove(path)


def test_logistics_printed_rtu_qr_handoff_and_receipt_bridge(monkeypatch) -> None:
    engine, path = setup_db()
    headers = _configure_logistics_auth(monkeypatch)
    app.dependency_overrides = {get_db: override_db(engine)}
    client = TestClient(app)

    _seed_reference_data(client, headers)
    ids = _id_maps(engine)

    rtu_sync = client.post(
        "/api/logistics/sync/units",
        json=[
            {
                "source_document_type": "rtu",
                "external_id": "0xb4fc002590803daf11f19eca3ecfe591",
                "document_number": "РБГУ0401217",
                "document_date": "2026-03-28T11:00:00Z",
                "source_warehouse_external_id": "store-1",
                "target_warehouse_external_id": "central",
                "document_target_warehouse_external_id": "central",
                "final_recipient_name": "Заказ 241666",
                "barcode": "RTU-BC-1",
                "lookup_code": ("MMLOG1|rtu|0xb4fc002590803daf11f19eca3ecfe591|241666"),
                "origin_order_external_id": "order-1c-1",
                "site_order_number": "241666",
                "status": "posted",
            }
        ],
        headers=headers,
    )
    assert rtu_sync.status_code == 200

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
    receipt_before_handoff = client.post(
        f"/api/logistics/receipts/draft/{receipt_id}/scan",
        json={
            "actor_user_id": ids["users"]["Получатель"],
            "lookup_code": "MMLOG1|rtu|83491597397407213546269390744020073903",
        },
        headers=headers,
    )
    assert receipt_before_handoff.status_code == 409
    assert receipt_before_handoff.json()["detail"] == (
        "Сначала выполните передачу водителю на складе отправления"
    )

    handoff_draft = client.post(
        "/api/logistics/handoffs/draft",
        json={
            "actor_user_id": ids["users"]["Отправитель"],
            "warehouse_id": ids["warehouses"]["store-1"],
            "driver_id": ids["drivers"]["Иван Водитель"],
            "default_dropoff_warehouse_id": ids["warehouses"]["central"],
        },
        headers=headers,
    )
    assert handoff_draft.status_code == 200
    draft_id = handoff_draft.json()["id"]
    assert (
        client.post(
            f"/api/logistics/handoffs/draft/{draft_id}/scan",
            json={
                "actor_user_id": ids["users"]["Отправитель"],
                "lookup_code": "MMLOG1|rtu|83491597397407213546269390744020073903",
            },
            headers=headers,
        ).status_code
        == 200
    )
    repeated_handoff_scan = client.post(
        f"/api/logistics/handoffs/draft/{draft_id}/scan",
        json={
            "actor_user_id": ids["users"]["Отправитель"],
            "lookup_code": "MMLOG1|rtu|83491597397407213546269390744020073903",
        },
        headers=headers,
    )
    assert repeated_handoff_scan.status_code == 200
    assert repeated_handoff_scan.json()["item_count"] == 1
    assert (
        client.post(
            f"/api/logistics/handoffs/draft/{draft_id}/confirm",
            json={"actor_user_id": ids["users"]["Отправитель"]},
            headers=headers,
        ).status_code
        == 200
    )

    assert (
        client.post(
            f"/api/logistics/receipts/draft/{receipt_id}/scan",
            json={
                "actor_user_id": ids["users"]["Получатель"],
                "lookup_code": ("MMLOG1|rtu|0xb4fc002590803daf11f19eca3ecfe591|241666"),
            },
            headers=headers,
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/logistics/receipts/draft/{receipt_id}/confirm",
            json={"actor_user_id": ids["users"]["Получатель"]},
            headers=headers,
        ).status_code
        == 200
    )

    with Session(engine) as session:
        case = session.scalar(select(SiteOrderExecutionCase))
        assert case is not None
        assert case.site_order_number == "241666"
        assert case.current_derived_status == "pickup_stored_at_point"
        assert case.current_crm_stage == "PICKUP_WAITING"
        event = session.scalar(select(SiteOrderExecutionEvent))
        assert event is not None
        assert event.source == "logistics"
        assert event.event_type != "pickup_client_received"
        stage_rows = session.scalars(
            select(SiteOrderStageOutbox).order_by(SiteOrderStageOutbox.id)
        ).all()
        assert [row.target_stage for row in stage_rows] == [
            "PICKUP_TRANSIT",
            "PICKUP_WAITING",
        ]
        assert [row.payload["source_channel"] for row in stage_rows] == [
            "api",
            "api",
        ]

    app.dependency_overrides = {}
    get_settings.cache_clear()
    get_engine.cache_clear()
    engine.dispose()
    if os.path.exists(path):
        os.remove(path)


def test_external_carrier_rtu_accept_does_not_create_pickup_storage(monkeypatch) -> None:
    engine, path = setup_db()
    headers = _configure_logistics_auth(monkeypatch)
    app.dependency_overrides = {get_db: override_db(engine)}
    client = TestClient(app)

    _seed_reference_data(client, headers)
    ids = _id_maps(engine)

    rtu_sync = client.post(
        "/api/logistics/sync/units",
        json=[
            {
                "source_document_type": "rtu",
                "external_id": "rtu-carrier-1",
                "document_number": "РТУ-EXT-1",
                "document_date": "2026-03-28T11:00:00Z",
                "source_warehouse_external_id": "store-1",
                "target_warehouse_external_id": "central",
                "document_target_warehouse_external_id": "central",
                "final_recipient_name": "Заказ 216952",
                "barcode": "MMLOG1|rtu|rtu-carrier-1|216952",
                "lookup_code": "MMLOG1|rtu|rtu-carrier-1|216952",
                "origin_order_external_id": "order-1c-2",
                "site_order_number": "216952",
                "status": "posted",
                "payload": {"external_carrier_flow": True},
            }
        ],
        headers=headers,
    )
    assert rtu_sync.status_code == 200

    with Session(engine) as session:
        transfer = session.scalar(
            select(LogisticsTransfer).where(LogisticsTransfer.external_id == "rtu-carrier-1")
        )
        assert transfer is not None
        result = logistics.handoff_to_external_carrier_from_sync(
            session,
            transfer_id=transfer.id,
            carrier_name="СДЭК",
            idempotency_key="test-carrier-1",
        )
        assert result["status"] == "created"
        transfer_id = transfer.id

    external_accept = client.post(
        f"/api/logistics/transfers/{transfer_id}/external-carrier/accept",
        json={
            "actor_user_id": ids["users"]["Логист"],
            "warehouse_id": ids["warehouses"]["central"],
        },
        headers=headers,
    )
    assert external_accept.status_code == 200

    with Session(engine) as session:
        assert session.scalar(select(SiteOrderExecutionEvent)) is None
        state = session.get(LogisticsTransferState, transfer_id)
        assert state is not None
        assert state.status == "at_warehouse"

    app.dependency_overrides = {}
    get_settings.cache_clear()
    get_engine.cache_clear()
    engine.dispose()
    if os.path.exists(path):
        os.remove(path)


def test_logistics_manual_override_and_review_security(monkeypatch) -> None:
    engine, path = setup_db()
    headers = _configure_logistics_auth(monkeypatch)
    app.dependency_overrides = {get_db: override_db(engine)}
    client = TestClient(app)

    _seed_reference_data(client, headers)
    ids = _id_maps(engine)

    forbidden = client.post(
        "/api/logistics/manual-ready-overrides",
        json={
            "actor_user_id": ids["users"]["Отправитель"],
            "source_document_type": "transfer",
            "external_id": "transfer-1",
            "warehouse_id": ids["warehouses"]["central"],
            "reason": "Тест",
        },
        headers=headers,
    )
    assert forbidden.status_code == 403

    override = client.post(
        "/api/logistics/manual-ready-overrides",
        json={
            "actor_user_id": ids["users"]["Логист"],
            "source_document_type": "transfer",
            "external_id": "transfer-1",
            "warehouse_id": ids["warehouses"]["central"],
            "reason": "Ручное подтверждение готовности",
        },
        headers=headers,
    )
    assert override.status_code == 200

    reviews = client.get("/api/logistics/manual-review", headers=headers)
    assert reviews.status_code == 200
    assert reviews.json()[0]["review_type"] == "manual_ready_override"

    with Session(engine) as session:
        assert session.query(LogisticsManualReview).count() == 1
        event = session.scalar(
            select(LogisticsTransferEvent).where(
                LogisticsTransferEvent.event_type == "manual_ready_override"
            )
        )
        assert event is not None
        state = session.get(LogisticsTransferState, ids["transfer_id"])
        assert state.current_warehouse_id == ids["warehouses"]["central"]

    app.dependency_overrides = {}
    get_settings.cache_clear()
    get_engine.cache_clear()
    engine.dispose()
    if os.path.exists(path):
        os.remove(path)


def test_logistics_web_fallback_session_uses_cookie(monkeypatch) -> None:
    engine, path = setup_db()
    headers = _configure_logistics_auth(monkeypatch)
    monkeypatch.setenv("LOGISTICS_WEB_SESSION_SECRET", "test-web-session-secret")
    monkeypatch.setenv("DEBUG", "true")
    get_settings.cache_clear()
    app.dependency_overrides = {get_db: override_db(engine)}
    client = TestClient(app)

    fallback_page = client.get("/logistics/fallback")
    assert fallback_page.status_code == 200
    assert 'href="./vite.svg"' not in fallback_page.text

    _seed_reference_data(client, headers)
    ids = _id_maps(engine)

    no_cookie = client.get("/api/logistics/web/profile")
    assert no_cookie.status_code == 401

    session_response = client.post(
        "/api/logistics/web/session",
        json={"actor_user_id": ids["users"]["Получатель"]},
        headers=headers,
    )
    assert session_response.status_code == 200
    assert "mm_logistics_session" in client.cookies

    profile = client.get("/api/logistics/web/profile")
    assert profile.status_code == 200
    assert profile.json()["full_name"] == "Получатель"

    web_monitor = client.get("/api/logistics/web/monitor")
    assert web_monitor.status_code == 200
    assert isinstance(web_monitor.json(), list)

    oversized_comment = client.post(
        "/api/logistics/web/receipts/draft",
        json={
            "warehouse_id": ids["warehouses"]["central"],
            "comment": "x" * 1001,
        },
    )
    assert oversized_comment.status_code == 422

    receipt_draft = client.post(
        "/api/logistics/web/receipts/draft",
        json={"warehouse_id": ids["warehouses"]["central"]},
    )
    assert receipt_draft.status_code == 200
    restored_draft = client.get("/api/logistics/web/draft/open")
    assert restored_draft.status_code == 200
    assert restored_draft.json()["id"] == receipt_draft.json()["id"]
    assert restored_draft.json()["draft_type"] == "receipt"

    malformed_photos = client.post(
        f"/api/logistics/web/receipts/draft/{receipt_draft.json()['id']}/confirm",
        json={"photos": [{}]},
    )
    assert malformed_photos.status_code == 422

    with Session(engine) as session:
        receiver = session.get(LogisticsUser, ids["users"]["Получатель"])
        assert receiver is not None
        receiver.default_warehouse_id = ids["warehouses"]["store-2"]
        session.commit()

    reassigned_warehouse = client.post(
        f"/api/logistics/web/receipts/draft/{receipt_draft.json()['id']}/scan",
        json={"barcode": "BC-0001"},
    )
    assert reassigned_warehouse.status_code == 403
    assert reassigned_warehouse.json()["detail"] == (
        "draft warehouse is outside current user assignment"
    )

    with Session(engine) as session:
        receiver = session.get(LogisticsUser, ids["users"]["Получатель"])
        assert receiver is not None
        receiver.role = "sender"
        session.commit()

    wrong_endpoint = client.post(
        f"/api/logistics/web/handoffs/draft/{receipt_draft.json()['id']}/scan",
        json={"barcode": "BC-0001"},
    )
    assert wrong_endpoint.status_code == 409
    assert wrong_endpoint.json()["detail"] == "draft type does not match endpoint"

    app.dependency_overrides = {}
    get_settings.cache_clear()
    get_engine.cache_clear()
    engine.dispose()
    if os.path.exists(path):
        os.remove(path)


def test_logistics_web_fallback_enforces_assigned_warehouse(monkeypatch) -> None:
    engine, path = setup_db()
    headers = _configure_logistics_auth(monkeypatch)
    monkeypatch.setenv("LOGISTICS_WEB_SESSION_SECRET", "test-web-session-secret")
    monkeypatch.setenv("DEBUG", "true")
    get_settings.cache_clear()
    app.dependency_overrides = {get_db: override_db(engine)}
    client = TestClient(app)

    _seed_reference_data(client, headers)
    ids = _id_maps(engine)
    session_response = client.post(
        "/api/logistics/web/session",
        json={"actor_user_id": ids["users"]["Получатель"]},
        headers=headers,
    )
    assert session_response.status_code == 200

    foreign_monitor = client.get(
        "/api/logistics/web/monitor",
        params={"warehouse_id": ids["warehouses"]["store-1"]},
    )
    assert foreign_monitor.status_code == 403

    foreign_draft = client.post(
        "/api/logistics/web/receipts/draft",
        json={"warehouse_id": ids["warehouses"]["store-1"]},
    )
    assert foreign_draft.status_code == 403

    with Session(engine) as session:
        receiver = session.get(LogisticsUser, ids["users"]["Получатель"])
        assert receiver is not None
        receiver.default_warehouse_id = None
        session.commit()

    missing_assignment = client.post(
        "/api/logistics/web/receipts/draft",
        json={"warehouse_id": ids["warehouses"]["central"]},
    )
    assert missing_assignment.status_code == 422

    app.dependency_overrides = {}
    get_settings.cache_clear()
    get_engine.cache_clear()
    engine.dispose()
    if os.path.exists(path):
        os.remove(path)


def test_logistics_draft_item_can_be_removed_and_draft_cancelled(monkeypatch) -> None:
    engine, path = setup_db()
    headers = _configure_logistics_auth(monkeypatch)
    app.dependency_overrides = {get_db: override_db(engine)}
    client = TestClient(app)

    _seed_reference_data(client, headers)
    ids = _id_maps(engine)
    route = client.post(
        "/api/logistics/route-runs",
        json={
            "route_name": "Рейс из черновика",
            "driver_id": ids["drivers"]["Иван Водитель"],
            "items": [],
        },
        headers=headers,
    )
    assert route.status_code == 200
    route_id = route.json()["id"]
    create_payload = {
        "actor_user_id": ids["users"]["Отправитель"],
        "warehouse_id": ids["warehouses"]["store-1"],
        "driver_id": ids["drivers"]["Иван Водитель"],
        "route_run_id": route_id,
        "default_dropoff_warehouse_id": ids["warehouses"]["central"],
    }
    draft = client.post(
        "/api/logistics/handoffs/draft",
        json=create_payload,
        headers=headers,
    )
    assert draft.status_code == 200
    draft_id = draft.json()["id"]
    scanned = client.post(
        f"/api/logistics/handoffs/draft/{draft_id}/scan",
        json={
            "actor_user_id": ids["users"]["Отправитель"],
            "lookup_code": "BC-0001",
        },
        headers=headers,
    )
    assert scanned.status_code == 200
    item_id = scanned.json()["items"][0]["id"]
    route_after_scan = client.get("/api/logistics/route-runs", headers=headers)
    assert route_after_scan.status_code == 200
    assert route_after_scan.json()[0]["items"] == []

    removed = client.post(
        f"/api/logistics/handoffs/draft/{draft_id}/items/{item_id}/remove",
        json={"actor_user_id": ids["users"]["Отправитель"]},
        headers=headers,
    )
    assert removed.status_code == 200
    assert removed.json()["item_count"] == 0

    cancelled = client.post(
        f"/api/logistics/handoffs/draft/{draft_id}/cancel",
        json={
            "actor_user_id": ids["users"]["Отправитель"],
            "reason": "Неверно выбран водитель",
        },
        headers=headers,
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["cancel_reason"] == "Неверно выбран водитель"
    assert cancelled.json()["cancelled_at"] is not None
    route_after_cancel = client.get("/api/logistics/route-runs", headers=headers)
    assert route_after_cancel.status_code == 200
    assert route_after_cancel.json()[0]["items"] == []

    confirm_cancelled = client.post(
        f"/api/logistics/handoffs/draft/{draft_id}/confirm",
        json={"actor_user_id": ids["users"]["Отправитель"]},
        headers=headers,
    )
    assert confirm_cancelled.status_code == 409

    replacement = client.post(
        "/api/logistics/handoffs/draft",
        json=create_payload,
        headers=headers,
    )
    assert replacement.status_code == 200
    assert replacement.json()["id"] != draft_id
    replacement_id = replacement.json()["id"]
    replacement_scan = client.post(
        f"/api/logistics/handoffs/draft/{replacement_id}/scan",
        json={
            "actor_user_id": ids["users"]["Отправитель"],
            "lookup_code": "BC-0001",
        },
        headers=headers,
    )
    assert replacement_scan.status_code == 200
    replacement_confirm = client.post(
        f"/api/logistics/handoffs/draft/{replacement_id}/confirm",
        json={"actor_user_id": ids["users"]["Отправитель"]},
        headers=headers,
    )
    assert replacement_confirm.status_code == 200
    route_after_confirm = client.get("/api/logistics/route-runs", headers=headers)
    assert route_after_confirm.status_code == 200
    assert route_after_confirm.json()[0]["items"][0]["status"] == "in_transit"

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
