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
    Base,
    LogisticsDriver,
    LogisticsManualReview,
    LogisticsOrderPlan,
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
            "actor_user_id": ids["users"]["Отправитель"],
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


def test_rtu_ready_for_pickup_returns_only_accepted_remote_rtu(monkeypatch) -> None:
    engine, path = setup_db()
    headers = _configure_logistics_auth(monkeypatch)
    app.dependency_overrides = {get_db: override_db(engine)}
    client = TestClient(app)

    try:
        with Session(engine) as session:
            source = LogisticsWarehouse(
                external_id="source",
                name="Пресня",
                kind="store",
                payload={"code": "PRS"},
            )
            target = LogisticsWarehouse(
                external_id="target",
                name="Савелово",
                kind="store",
                payload={"onec_departments": [{"code": "SAV"}]},
            )
            session.add_all([source, target])
            session.flush()
            accepted_at = datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)
            remote = LogisticsTransfer(
                source_document_type="rtu",
                external_id="0xRTU-REMOTE",
                document_number="РТУ-000321",
                document_date=datetime(2026, 8, 27, 9, 0, tzinfo=timezone.utc),
                source_warehouse_id=source.id,
                target_warehouse_id=target.id,
                barcode="MMLOG1|rtu|remote",
            )
            local = LogisticsTransfer(
                source_document_type="rtu",
                external_id="0xRTU-LOCAL",
                document_number="РТУ-000322",
                document_date=datetime(2026, 8, 27, 10, 0, tzinfo=timezone.utc),
                source_warehouse_id=target.id,
                target_warehouse_id=target.id,
                barcode="MMLOG1|rtu|local",
            )
            session.add_all([remote, local])
            session.flush()
            session.add_all(
                [
                    LogisticsTransferState(
                        transfer_id=remote.id,
                        status=logistics.STATUS_AT_WAREHOUSE,
                        current_warehouse_id=target.id,
                        last_event_type=logistics.EVENT_ACCEPTED_AT_POINT,
                        last_event_at=accepted_at,
                        version=2,
                    ),
                    LogisticsTransferState(
                        transfer_id=local.id,
                        status=logistics.STATUS_AT_WAREHOUSE,
                        current_warehouse_id=target.id,
                        last_event_type=logistics.EVENT_ACCEPTED_AT_POINT,
                        last_event_at=accepted_at,
                        version=2,
                    ),
                ]
            )
            session.commit()

        response = client.get(
            "/api/logistics/rtu/ready-for-pickup",
            params={"warehouse_code": "sav", "date_from": "2026-08-26"},
            headers=headers,
        )
        assert response.status_code == 200
        assert [item["external_id"] for item in response.json()] == ["0xRTU-REMOTE"]

        xml_response = client.get(
            "/api/logistics/rtu/ready-for-pickup",
            params={"warehouse_code": "SAV", "format": "xml"},
            headers=headers,
        )
        assert xml_response.status_code == 200
        assert xml_response.headers["content-type"].startswith("application/xml")
        assert "РТУ-000321" in xml_response.text
        assert "РТУ-000322" not in xml_response.text

        missing = client.get(
            "/api/logistics/rtu/ready-for-pickup",
            params={"warehouse_code": "UNKNOWN"},
            headers=headers,
        )
        assert missing.status_code == 404
    finally:
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
            "actor_user_id": ids["users"]["Отправитель"],
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


def test_logistics_rtu_receipt_bridges_to_order_fulfillment(monkeypatch) -> None:
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
                "external_id": "rtu-1",
                "document_number": "РТУ-000001",
                "document_date": "2026-03-28T11:00:00Z",
                "source_warehouse_external_id": "store-1",
                "target_warehouse_external_id": "central",
                "document_target_warehouse_external_id": "central",
                "final_recipient_name": "Заказ 216951",
                "barcode": "RTU-BC-1",
                "lookup_code": "MMLOG1|rtu|rtu-1|216951",
                "origin_order_external_id": "order-1c-1",
                "site_order_number": "216951",
                "status": "posted",
            }
        ],
        headers=headers,
    )
    assert rtu_sync.status_code == 200

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
                "lookup_code": "MMLOG1|rtu|rtu-1|216951",
            },
            headers=headers,
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/logistics/handoffs/draft/{draft_id}/confirm",
            json={"actor_user_id": ids["users"]["Отправитель"]},
            headers=headers,
        ).status_code
        == 200
    )

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
    assert (
        client.post(
            f"/api/logistics/receipts/draft/{receipt_id}/scan",
            json={
                "actor_user_id": ids["users"]["Получатель"],
                "lookup_code": "MMLOG1|rtu|rtu-1|216951",
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
        assert case.site_order_number == "216951"
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
            "telegram",
            "telegram",
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

    app.dependency_overrides = {}
    get_settings.cache_clear()
    get_engine.cache_clear()
    engine.dispose()
    if os.path.exists(path):
        os.remove(path)


def test_order_transfer_plan_gates_handoff_and_aggregates_pickup(monkeypatch) -> None:
    engine, path = setup_db()
    headers = _configure_logistics_auth(monkeypatch)
    app.dependency_overrides = {get_db: override_db(engine)}
    client = TestClient(app)

    _seed_reference_data(client, headers)
    assert (
        client.post(
            "/api/logistics/sync/users",
            json=[
                {
                    "external_id": "user-sender-store-2",
                    "full_name": "Отправитель 2",
                    "role": "sender",
                    "default_warehouse_external_id": "store-2",
                }
            ],
            headers=headers,
        ).status_code
        == 200
    )
    ids = _id_maps(engine)
    plan_units = [
        {
            "unit_key": "source-a-to-central",
            "source_warehouse_external_id": "store-1",
            "target_warehouse_external_id": "central",
            "internal_order_external_id": "internal-order-a",
            "transfer_external_id": "transfer-order-a",
            "is_required": True,
            "ready_for_handoff": False,
            "readiness": "assembling",
        },
        {
            "unit_key": "source-b-to-central",
            "source_warehouse_external_id": "store-2",
            "target_warehouse_external_id": "central",
            "internal_order_external_id": "internal-order-b",
            "transfer_external_id": "transfer-order-b",
            "is_required": True,
            "ready_for_handoff": False,
            "readiness": "assembling",
        },
    ]
    plan_payload = {
        "origin_order_external_id": "customer-order-1",
        "site_order_number": "250001",
        "flow_mode": "ORDER_TRANSFER_V1",
        "plan_key": "customer-order-1:central",
        "plan_version": 1,
        "final_warehouse_external_id": "central",
        "status": "assembling",
        "expected_unit_count": 2,
        "units": plan_units,
    }
    assert (
        client.post(
            "/api/logistics/sync/order-plans", json=[plan_payload], headers=headers
        ).status_code
        == 200
    )

    def unit_payload(external_id: str, unit_key: str, source: str, ready: bool) -> dict:
        return {
            "source_document_type": "transfer",
            "external_id": external_id,
            "document_number": external_id,
            "document_date": "2026-09-02T09:00:00Z",
            "source_warehouse_external_id": source,
            "target_warehouse_external_id": "central",
            "document_target_warehouse_external_id": "central",
            "barcode": f"QR-{external_id}",
            "lookup_code": f"QR-{external_id}",
            "origin_order_external_id": "customer-order-1",
            "site_order_number": "250001",
            "status": "posted",
            "flow_mode": "ORDER_TRANSFER_V1",
            "plan_key": "customer-order-1:central",
            "plan_version": 1,
            "unit_key": unit_key,
            "expected_unit_count": 2,
            "ready_for_handoff": ready,
            "is_required": True,
            "payload": {"readiness": "ready" if ready else "assembling"},
        }

    units = [
        unit_payload("transfer-order-a", "source-a-to-central", "store-1", False),
        unit_payload("transfer-order-b", "source-b-to-central", "store-2", False),
    ]
    assert client.post("/api/logistics/sync/units", json=units, headers=headers).status_code == 200

    blocked_draft = client.post(
        "/api/logistics/handoffs/draft",
        json={
            "actor_user_id": ids["users"]["Отправитель"],
            "warehouse_id": ids["warehouses"]["store-1"],
            "driver_id": ids["drivers"]["Иван Водитель"],
            "default_dropoff_warehouse_id": ids["warehouses"]["central"],
        },
        headers=headers,
    )
    blocked_draft_id = blocked_draft.json()["id"]
    blocked_scan = client.post(
        f"/api/logistics/handoffs/draft/{blocked_draft_id}/scan",
        json={
            "actor_user_id": ids["users"]["Отправитель"],
            "lookup_code": "QR-transfer-order-a",
        },
        headers=headers,
    )
    assert blocked_scan.status_code == 409
    assert "not posted, printed and assembled" in blocked_scan.text

    ready_plan = dict(plan_payload)
    ready_plan["status"] = "ready_for_handoff"
    ready_plan["units"] = [
        {**unit, "ready_for_handoff": True, "readiness": "ready"} for unit in plan_units
    ]
    assert (
        client.post(
            "/api/logistics/sync/order-plans", json=[ready_plan], headers=headers
        ).status_code
        == 200
    )
    ready_units = [
        unit_payload("transfer-order-a", "source-a-to-central", "store-1", True),
        unit_payload("transfer-order-b", "source-b-to-central", "store-2", True),
    ]
    assert (
        client.post("/api/logistics/sync/units", json=ready_units, headers=headers).status_code
        == 200
    )

    # Упаковки могут ехать одним рейсом, но передаются со своих складов отдельно.
    for external_id, source in (
        ("transfer-order-a", "store-1"),
        ("transfer-order-b", "store-2"),
    ):
        draft = (
            blocked_draft
            if source == "store-1"
            else client.post(
                "/api/logistics/handoffs/draft",
                json={
                    "actor_user_id": ids["users"]["Отправитель 2"],
                    "warehouse_id": ids["warehouses"][source],
                    "driver_id": ids["drivers"]["Иван Водитель"],
                    "default_dropoff_warehouse_id": ids["warehouses"]["central"],
                },
                headers=headers,
            )
        )
        draft_id = draft.json()["id"]
        actor_id = (
            ids["users"]["Отправитель"] if source == "store-1" else ids["users"]["Отправитель 2"]
        )
        assert (
            client.post(
                f"/api/logistics/handoffs/draft/{draft_id}/scan",
                json={
                    "actor_user_id": actor_id,
                    "lookup_code": f"QR-{external_id}",
                },
                headers=headers,
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/api/logistics/handoffs/draft/{draft_id}/confirm",
                json={"actor_user_id": actor_id, "idempotency_key": external_id},
                headers=headers,
            ).status_code
            == 200
        )

    with Session(engine) as session:
        transit_rows = session.scalars(
            select(SiteOrderStageOutbox).where(
                SiteOrderStageOutbox.target_stage == "PICKUP_TRANSIT"
            )
        ).all()
        assert len(transit_rows) == 1

    for index, external_id in enumerate(("transfer-order-a", "transfer-order-b")):
        receipt = client.post(
            "/api/logistics/receipts/draft",
            json={
                "actor_user_id": ids["users"]["Получатель"],
                "warehouse_id": ids["warehouses"]["central"],
            },
            headers=headers,
        )
        receipt_id = receipt.json()["id"]
        assert (
            client.post(
                f"/api/logistics/receipts/draft/{receipt_id}/scan",
                json={
                    "actor_user_id": ids["users"]["Получатель"],
                    "lookup_code": f"QR-{external_id}",
                },
                headers=headers,
            ).status_code
            == 200
        )
        confirmed = client.post(
            f"/api/logistics/receipts/draft/{receipt_id}/confirm",
            json={
                "actor_user_id": ids["users"]["Получатель"],
                "idempotency_key": f"receipt-{external_id}",
            },
            headers=headers,
        )
        assert confirmed.status_code == 200
        with Session(engine) as session:
            waiting_count = len(
                session.scalars(
                    select(SiteOrderStageOutbox).where(
                        SiteOrderStageOutbox.target_stage == "PICKUP_WAITING"
                    )
                ).all()
            )
            assert waiting_count == index

    ready = client.get(
        "/api/logistics/orders/ready-for-pickup",
        params={"warehouse_code": "central"},
        headers=headers,
    )
    assert ready.status_code == 200
    assert ready.json() == [
        {
            "origin_order_external_id": "customer-order-1",
            "site_order_number": "250001",
            "flow_mode": "ORDER_TRANSFER_V1",
            "plan_key": "customer-order-1:central",
            "plan_version": 1,
            "ready_at": ready.json()[0]["ready_at"],
            "expected_unit_count": 2,
            "accepted_unit_count": 2,
            "source_external_ids": ["transfer-order-a", "transfer-order-b"],
        }
    ]
    with Session(engine) as session:
        order_plan = session.scalar(
            select(LogisticsOrderPlan).where(
                LogisticsOrderPlan.origin_order_external_id == "customer-order-1"
            )
        )
        order_plan.synced_at = logistics.utcnow() - timedelta(
            seconds=logistics.ORDER_PLAN_SYNC_MAX_AGE_SECONDS + 1
        )
        session.commit()
    assert (
        client.get(
            "/api/logistics/orders/ready-for-pickup",
            params={"warehouse_code": "central"},
            headers=headers,
        ).json()
        == []
    )
    assert (
        client.post(
            "/api/logistics/sync/order-plans", json=[ready_plan], headers=headers
        ).status_code
        == 200
    )
    assert (
        client.get(
            "/api/logistics/rtu/ready-for-pickup",
            params={"warehouse_code": "central"},
            headers=headers,
        ).json()
        == []
    )
    with Session(engine) as session:
        logistics._create_order_flow_conflict(
            session,
            origin_order_external_id="customer-order-1",
            site_order_number="250001",
            reason="test unresolved dual-flow conflict",
            payload={"test": True},
        )
        session.commit()
    assert (
        client.get(
            "/api/logistics/orders/ready-for-pickup",
            params={"warehouse_code": "central"},
            headers=headers,
        ).json()
        == []
    )

    app.dependency_overrides = {}
    get_settings.cache_clear()
    get_engine.cache_clear()
    engine.dispose()
    if os.path.exists(path):
        os.remove(path)


def test_order_transfer_external_carrier_uses_bitrix_confirmation(monkeypatch) -> None:
    engine, path = setup_db()
    headers = _configure_logistics_auth(monkeypatch)
    app.dependency_overrides = {get_db: override_db(engine)}
    client = TestClient(app)

    _seed_reference_data(client, headers)
    ids = _id_maps(engine)
    plan = {
        "origin_order_external_id": "customer-order-cdek",
        "site_order_number": "250777",
        "flow_mode": "ORDER_TRANSFER_V1",
        "plan_key": "customer-order-cdek:central",
        "plan_version": 1,
        "final_warehouse_external_id": "central",
        "status": "ready_for_handoff",
        "expected_unit_count": 1,
        "payload": {"delivery_code": "CDEK_PVZ", "external_carrier": True},
        "units": [
            {
                "unit_key": "store-1-to-cdek",
                "source_warehouse_external_id": "store-1",
                "target_warehouse_external_id": "central",
                "internal_order_external_id": "internal-cdek",
                "transfer_external_id": "transfer-cdek",
                "is_required": True,
                "ready_for_handoff": True,
                "readiness": "ready",
            }
        ],
    }
    assert (
        client.post("/api/logistics/sync/order-plans", json=[plan], headers=headers).status_code
        == 200
    )
    transfer = {
        "source_document_type": "transfer",
        "external_id": "transfer-cdek",
        "document_number": "ТР-250777",
        "document_date": "2026-09-02T09:00:00Z",
        "source_warehouse_external_id": "store-1",
        "target_warehouse_external_id": "central",
        "document_target_warehouse_external_id": "central",
        "barcode": "QR-transfer-cdek",
        "lookup_code": "QR-transfer-cdek",
        "origin_order_external_id": "customer-order-cdek",
        "site_order_number": "250777",
        "status": "posted",
        "flow_mode": "ORDER_TRANSFER_V1",
        "plan_key": "customer-order-cdek:central",
        "plan_version": 1,
        "unit_key": "store-1-to-cdek",
        "expected_unit_count": 1,
        "ready_for_handoff": True,
        "is_required": True,
        "payload": {"readiness": "ready"},
    }
    assert (
        client.post("/api/logistics/sync/units", json=[transfer], headers=headers).status_code
        == 200
    )

    handoff = client.post(
        "/api/logistics/handoffs/draft",
        json={
            "actor_user_id": ids["users"]["Отправитель"],
            "warehouse_id": ids["warehouses"]["store-1"],
            "driver_id": ids["drivers"]["Иван Водитель"],
            "default_dropoff_warehouse_id": ids["warehouses"]["central"],
        },
        headers=headers,
    )
    handoff_id = handoff.json()["id"]
    assert (
        client.post(
            f"/api/logistics/handoffs/draft/{handoff_id}/scan",
            json={
                "actor_user_id": ids["users"]["Отправитель"],
                "lookup_code": "QR-transfer-cdek",
            },
            headers=headers,
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/logistics/handoffs/draft/{handoff_id}/confirm",
            json={
                "actor_user_id": ids["users"]["Отправитель"],
                "idempotency_key": "cdek-handoff",
            },
            headers=headers,
        ).status_code
        == 200
    )

    receipt = client.post(
        "/api/logistics/receipts/draft",
        json={
            "actor_user_id": ids["users"]["Получатель"],
            "warehouse_id": ids["warehouses"]["central"],
        },
        headers=headers,
    )
    blocked = client.post(
        f"/api/logistics/receipts/draft/{receipt.json()['id']}/scan",
        json={
            "actor_user_id": ids["users"]["Получатель"],
            "lookup_code": "QR-transfer-cdek",
        },
        headers=headers,
    )
    assert blocked.status_code == 409
    assert "existing Bitrix integration" in blocked.text

    confirmation = {
        "origin_order_external_id": "customer-order-cdek",
        "site_order_number": "250777",
        "carrier_name": "СДЭК",
        "tracking_number": "CDEK-250777",
        "confirmed_at": "2026-09-02T10:00:00Z",
        "source_ref": "bitrix-deal-250777-track-CDEK-250777",
        "terminal_warehouse_external_id": "central",
    }
    missing_terminal = dict(confirmation)
    missing_terminal.pop("terminal_warehouse_external_id")
    assert (
        client.post(
            "/api/logistics/sync/carrier-confirmations",
            json=[missing_terminal],
            headers=headers,
        ).status_code
        == 422
    )
    with Session(engine) as session:
        order_plan = session.scalar(
            select(LogisticsOrderPlan).where(
                LogisticsOrderPlan.origin_order_external_id == "customer-order-cdek"
            )
        )
        order_plan.synced_at = logistics.utcnow() - timedelta(
            seconds=logistics.ORDER_PLAN_SYNC_MAX_AGE_SECONDS + 1
        )
        session.commit()
    stale = client.post(
        "/api/logistics/sync/carrier-confirmations",
        json=[confirmation],
        headers=headers,
    )
    assert stale.status_code == 409
    assert "sync is stale" in stale.text
    assert (
        client.post("/api/logistics/sync/order-plans", json=[plan], headers=headers).status_code
        == 200
    )
    first = client.post(
        "/api/logistics/sync/carrier-confirmations",
        json=[confirmation],
        headers=headers,
    )
    assert first.status_code == 200
    assert first.json() == {"created": 1, "updated": 0}
    repeat = client.post(
        "/api/logistics/sync/carrier-confirmations",
        json=[confirmation],
        headers=headers,
    )
    assert repeat.status_code == 200
    assert repeat.json() == {"created": 0, "updated": 1}
    assert (
        client.post("/api/logistics/sync/order-plans", json=[plan], headers=headers).status_code
        == 200
    )

    feed = client.get(
        "/api/logistics/orders/carrier-confirmations",
        headers=headers,
    )
    assert feed.status_code == 200
    assert feed.json()[0]["tracking_number"] == "CDEK-250777"
    assert feed.json()[0]["origin_order_external_id"] == "customer-order-cdek"
    xml_feed = client.get(
        "/api/logistics/orders/carrier-confirmations",
        params={"format": "xml", "confirmed_from": "2026-09-02T09:59:00Z"},
        headers=headers,
    )
    assert xml_feed.status_code == 200
    assert xml_feed.headers["content-type"].startswith("application/xml")
    assert b"<carrier_confirmations>" in xml_feed.content
    assert b"<tracking_number>CDEK-250777</tracking_number>" in xml_feed.content
    assert b"<plan_version>1</plan_version>" in xml_feed.content
    assert (
        client.get(
            "/api/logistics/orders/ready-for-pickup",
            params={"warehouse_code": "central"},
            headers=headers,
        ).json()
        == []
    )

    final_rtu = {
        **transfer,
        "source_document_type": "rtu",
        "external_id": "final-rtu-cdek",
        "document_number": "РТУ-250777",
        "flow_mode": None,
        "plan_key": None,
        "plan_version": None,
        "unit_key": None,
    }
    final_sync = client.post("/api/logistics/sync/units", json=[final_rtu], headers=headers)
    assert final_sync.status_code == 200
    assert final_sync.json() == {"created": 0, "updated": 0}
    with Session(engine) as session:
        assert session.scalar(select(SiteOrderStageOutbox)) is None

    app.dependency_overrides = {}
    get_settings.cache_clear()
    get_engine.cache_clear()
    engine.dispose()
    if os.path.exists(path):
        os.remove(path)


def test_order_transfer_carrier_confirmation_blocks_ambiguous_order_link(monkeypatch) -> None:
    engine, path = setup_db()
    headers = _configure_logistics_auth(monkeypatch)
    app.dependency_overrides = {get_db: override_db(engine)}
    client = TestClient(app)

    _seed_reference_data(client, headers)

    def plan(origin_order_external_id: str, site_order_number: str) -> dict:
        return {
            "origin_order_external_id": origin_order_external_id,
            "site_order_number": site_order_number,
            "flow_mode": "ORDER_TRANSFER_V1",
            "plan_key": f"{origin_order_external_id}:central",
            "plan_version": 1,
            "final_warehouse_external_id": "central",
            "status": "ready_for_handoff",
            "expected_unit_count": 0,
            "payload": {"delivery_code": "CDEK_PVZ", "external_carrier": True},
            "units": [],
        }

    assert (
        client.post(
            "/api/logistics/sync/order-plans",
            json=[plan("customer-order-a", "site-order-a")],
            headers=headers,
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/logistics/sync/order-plans",
            json=[plan("customer-order-b", "site-order-b")],
            headers=headers,
        ).status_code
        == 200
    )
    confirmation = {
        "origin_order_external_id": "customer-order-a",
        "site_order_number": "site-order-b",
        "carrier_name": "СДЭК",
        "tracking_number": "CDEK-AMBIGUOUS",
        "confirmed_at": "2026-09-02T10:00:00Z",
        "source_ref": "bitrix-ambiguous-link",
        "terminal_warehouse_external_id": "central",
    }
    ambiguous = client.post(
        "/api/logistics/sync/carrier-confirmations",
        json=[confirmation],
        headers=headers,
    )
    assert ambiguous.status_code == 409
    assert "ambiguous" in ambiguous.text

    with Session(engine) as session:
        review = session.scalar(
            select(LogisticsManualReview).where(
                LogisticsManualReview.review_type == "order_flow_conflict",
                LogisticsManualReview.status == "open",
            )
        )
        assert review is not None
        assert review.source_external_id == "customer-order-a"

    confirmation["site_order_number"] = "site-order-a"
    confirmation["source_ref"] = "bitrix-correct-after-conflict"
    blocked = client.post(
        "/api/logistics/sync/carrier-confirmations",
        json=[confirmation],
        headers=headers,
    )
    assert blocked.status_code == 409
    assert "unresolved flow conflict" in blocked.text

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
