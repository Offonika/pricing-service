from __future__ import annotations

from collections.abc import Generator

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api import order_fulfillment as order_fulfillment_api
from app.api.dependencies import get_db
from app.core.config import get_settings
from app.main import app
from app.models.site_order_fulfillment import (
    BitrixChatMention,
    BitrixChatMessage,
    SiteOrderExecutionCase,
    SiteOrderExecutionEvent,
    SiteOrderStageOutbox,
)
from app.services import site_order_fulfillment as service
from app.services import site_order_shipments as shipment_service

SITE_ORDER_TABLES = [
    SiteOrderExecutionCase.__table__,
    BitrixChatMessage.__table__,
    BitrixChatMention.__table__,
    SiteOrderExecutionEvent.__table__,
]


def _headers(token: str = "order-token") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _override_db(engine):
    def override() -> Generator[Session, None, None]:
        session = Session(engine)
        try:
            yield session
        finally:
            session.close()

    return override


def _configure(monkeypatch, token: str = "order-token") -> None:
    monkeypatch.setenv("ORDER_FULFILLMENT_INTERNAL_API_TOKEN", token)
    monkeypatch.setenv("ORDER_FULFILLMENT_BITRIX_WEBHOOK_URL", "")
    monkeypatch.setenv("ONEC_DATABASE_URL", "")
    monkeypatch.setenv("ORDER_FULFILLMENT_BOT_ENABLED", "true")
    monkeypatch.delenv("LOGISTICS_INTERNAL_API_TOKEN", raising=False)
    monkeypatch.delenv("MANAGEMENT_INTERNAL_API_TOKEN", raising=False)
    get_settings.cache_clear()


def test_order_fulfillment_message_endpoint_requires_token(monkeypatch) -> None:
    _configure(monkeypatch)
    client = TestClient(app)

    response = client.post(
        "/api/order-fulfillment/bitrix/messages",
        json={
            "chat_code": service.CHAT_SITE_MASTER_MOBILE,
            "dialog_id": "chat733",
            "chat_id": 733,
            "message_id": 1,
            "text": "218014 не забрали",
            "dry_run": True,
        },
    )

    assert response.status_code == 401


def test_order_fulfillment_message_endpoint_dry_run_parses_without_db(monkeypatch) -> None:
    _configure(monkeypatch)
    client = TestClient(app)

    response = client.post(
        "/api/order-fulfillment/bitrix/messages",
        json={
            "chat_code": service.CHAT_SITE_MASTER_MOBILE,
            "dialog_id": "chat733",
            "chat_id": 733,
            "message_id": 1,
            "text": "218014\n217624\nне забрали спб садовая",
            "dry_run": True,
        },
        headers=_headers(),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["parse_status"] == "parsed"
    assert payload["events_created"] == 0
    assert [item["site_order_number"] for item in payload["mentions"]] == [
        "218014",
        "217624",
    ]


def test_onec_execution_event_ingest_is_gated_and_idempotent(monkeypatch) -> None:
    _configure(monkeypatch)
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SiteOrderExecutionCase.__table__.create(engine)
    SiteOrderExecutionEvent.__table__.create(engine)
    app.dependency_overrides = {get_db: _override_db(engine)}
    client = TestClient(app)
    request = {
        "signal": "issued",
        "event_at": "2026-09-04T12:30:00+03:00",
        "site_order_number": "245383",
        "onec_order_number": "РБГУ0063466",
        "rtu_number": "РБГУ0197082",
        "is_posted": True,
        "document_amount": "1560.00",
    }
    try:
        blocked = client.post(
            "/api/order-fulfillment/execution/events",
            json=request,
            headers=_headers(),
        )
        assert blocked.status_code == 409

        dry_run = client.post(
            "/api/order-fulfillment/execution/events",
            json={**request, "dry_run": True},
            headers=_headers(),
        )
        assert dry_run.status_code == 200
        assert dry_run.json()["event_id"] is None

        monkeypatch.setenv("ORDER_FULFILLMENT_EXECUTION_MASTER_ENABLED", "true")
        monkeypatch.setenv("ORDER_FULFILLMENT_EXECUTION_INGEST_ENABLED", "true")
        monkeypatch.setenv("ORDER_FULFILLMENT_EXECUTION_RECONCILIATION_ENABLED", "true")
        reconciled: list[tuple[str, bool]] = []
        monkeypatch.setattr(
            order_fulfillment_api,
            "_reconcile_direct_execution_event",
            lambda order, *, apply: reconciled.append((order, apply)),
        )
        get_settings.cache_clear()
        first = client.post(
            "/api/order-fulfillment/execution/events",
            json=request,
            headers=_headers(),
        )
        second = client.post(
            "/api/order-fulfillment/execution/events",
            json=request,
            headers=_headers(),
        )
    finally:
        app.dependency_overrides = {}
        get_settings.cache_clear()

    assert first.status_code == 200
    assert first.json()["duplicate"] is False
    assert first.json()["reconciliation_queued"] is True
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert second.json()["reconciliation_queued"] is False
    assert reconciled == [("245383", False)]
    with Session(engine) as session:
        assert len(session.scalars(select(SiteOrderExecutionEvent)).all()) == 1


def test_site_crm_signal_ingest_is_gated_idempotent_and_queues_narrow_processing(
    monkeypatch,
) -> None:
    _configure(monkeypatch)
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SiteOrderExecutionCase.__table__.create(engine)
    SiteOrderExecutionEvent.__table__.create(engine)
    SiteOrderStageOutbox.__table__.create(engine)
    app.dependency_overrides = {get_db: _override_db(engine)}
    client = TestClient(app)
    request = {
        "signal": "delivered",
        "event_at": "2026-09-04T13:30:00+03:00",
        "site_order_number": "245388",
        "bitrix_deal_id": 39002,
        "source_revision": "tracking-sha256",
        "current_stage": "IN_DELIVERY",
    }
    try:
        blocked = client.post(
            "/api/order-fulfillment/site/events",
            json=request,
            headers=_headers(),
        )
        assert blocked.status_code == 409

        dry_run = client.post(
            "/api/order-fulfillment/site/events",
            json={**request, "dry_run": True},
            headers=_headers(),
        )
        assert dry_run.status_code == 200
        assert dry_run.json()["event_id"] is None

        monkeypatch.setenv("ORDER_FULFILLMENT_EXECUTION_MASTER_ENABLED", "true")
        monkeypatch.setenv("ORDER_FULFILLMENT_SITE_SIGNAL_INGEST_ENABLED", "true")
        processed: list[tuple[str, bool]] = []
        monkeypatch.setattr(
            order_fulfillment_api,
            "_process_direct_site_signal",
            lambda order, *, apply: processed.append((order, apply)),
        )
        get_settings.cache_clear()
        first = client.post(
            "/api/order-fulfillment/site/events",
            json=request,
            headers=_headers(),
        )
        second = client.post(
            "/api/order-fulfillment/site/events",
            json=request,
            headers=_headers(),
        )
    finally:
        app.dependency_overrides = {}
        get_settings.cache_clear()

    assert first.status_code == 200
    assert first.json()["duplicate"] is False
    assert first.json()["reconciliation_queued"] is True
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert processed == [("245388", False)]
    with Session(engine) as session:
        event = session.scalar(select(SiteOrderExecutionEvent))
        outbox = session.scalar(select(SiteOrderStageOutbox))
        case = session.scalar(select(SiteOrderExecutionCase))
        assert event is not None and event.event_type == "site_carrier_delivered"
        assert outbox is not None and outbox.target_stage == "WON"
        assert outbox.bitrix_deal_id == 39002
        assert case is not None and case.bitrix_deal_id == 39002
        assert case.current_crm_stage == "IN_DELIVERY"
        assert case.payload["site_crm_signal"]["review_required"] is False


def test_shipment_sync_dry_run_reports_partial_assembly_without_db(monkeypatch) -> None:
    _configure(monkeypatch)
    client = TestClient(app)

    response = client.post(
        "/api/order-fulfillment/shipments/sync",
        json={
            "site_order_number": "242685",
            "bitrix_deal_id": 39001,
            "current_stage": "EXECUTING",
            "event_at": "2026-08-29T12:00:00+03:00",
            "expected_items": [
                {"product_ref": "phone", "quantity": "1"},
                {"product_ref": "case", "quantity": "1"},
            ],
            "rtus": [
                {
                    "external_id": "rtu-1",
                    "posted": True,
                    "assembled_at": "2026-08-29T11:00:00+03:00",
                    "items": [{"product_ref": "phone", "quantity": "1"}],
                }
            ],
            "shipments": [],
            "dry_run": True,
        },
        headers=_headers(),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["coverage_status"] == "partial"
    assert payload["full_assembly"] is False
    assert payload["action"] == "noop"


def test_shipment_sync_persist_is_feature_gated(monkeypatch) -> None:
    _configure(monkeypatch)
    client = TestClient(app)

    response = client.post(
        "/api/order-fulfillment/shipments/sync",
        json={
            "site_order_number": "242685",
            "bitrix_deal_id": 39001,
            "event_at": "2026-08-29T12:00:00+03:00",
            "expected_items": [{"product_ref": "phone", "quantity": "1"}],
            "dry_run": False,
        },
        headers=_headers(),
    )

    assert response.status_code == 409


def test_shipment_sync_enqueues_only_explicit_missing_part_for_gateway(
    monkeypatch,
) -> None:
    _configure(monkeypatch)
    monkeypatch.setenv("ORDER_FULFILLMENT_SHIPMENTS_MASTER_ENABLED", "true")
    monkeypatch.setenv("ORDER_FULFILLMENT_SHIPMENTS_INGEST_ENABLED", "true")
    monkeypatch.setenv("ORDER_FULFILLMENT_SHIPMENTS_GATEWAY_APPLY_ENABLED", "true")
    monkeypatch.setenv("ORDER_FULFILLMENT_SHIPMENTS_GATEWAY_URL", "https://example.invalid")
    monkeypatch.setenv("ORDER_FULFILLMENT_SHIPMENTS_GATEWAY_TOKEN", "secret")
    get_settings.cache_clear()
    captured: dict = {}

    def fake_sync(db, **kwargs):
        del db
        captured["sync"] = kwargs
        return shipment_service.ShipmentSyncResult(
            snapshot_id="a" * 64,
            site_order_number=kwargs["site_order_number"],
            coverage_status="complete",
            full_assembly=True,
            shipment_count=2,
            target_stage="FINAL_INVOICE",
            action="update_stage",
            reason="all_order_quantities_assembled",
            gateway_operation_count=1,
        )

    monkeypatch.setattr(shipment_service, "sync_order_shipments", fake_sync)
    engine = create_engine("sqlite://", poolclass=StaticPool)
    app.dependency_overrides = {get_db: _override_db(engine)}
    client = TestClient(app)
    try:
        response = client.post(
            "/api/order-fulfillment/shipments/sync",
            json={
                "site_order_number": "242685",
                "bitrix_deal_id": 39001,
                "bitrix_order_id": 7001,
                "current_stage": "EXECUTING",
                "event_at": "2026-08-29T12:00:00+03:00",
                "expected_items": [
                    {"product_ref": "phone", "quantity": "1"},
                    {"product_ref": "case", "quantity": "1"},
                ],
                "rtus": [],
                "shipments": [
                    {
                        "shipment_key": "part-1",
                        "bitrix_shipment_id": 51,
                        "delivery_service_id": 11,
                        "items": [
                            {
                                "product_ref": "phone",
                                "basket_item_id": 701,
                                "quantity": "1",
                            }
                        ],
                    },
                    {
                        "shipment_key": "part-2",
                        "delivery_service_id": 11,
                        "explicit_split_confirmed": True,
                        "items": [
                            {
                                "product_ref": "case",
                                "basket_item_id": 702,
                                "quantity": "1",
                            }
                        ],
                    },
                ],
                "dry_run": False,
            },
            headers=_headers(),
        )
    finally:
        app.dependency_overrides = {}
        get_settings.cache_clear()

    assert response.status_code == 200
    assert response.json()["gateway_operation_count"] == 1
    assert captured["sync"]["enqueue_gateway"] is True
    assert captured["sync"]["shipments"][0]["bitrix_shipment_id"] == 51
    assert captured["sync"]["shipments"][1]["explicit_split_confirmed"] is True
    assert captured["sync"]["shipments"][1]["bitrix_shipment_id"] is None


def test_order_fulfillment_persisted_courier_ocr_creates_recommendation(
    monkeypatch,
) -> None:
    _configure(monkeypatch)
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    for table in SITE_ORDER_TABLES:
        table.create(engine)
    app.dependency_overrides = {get_db: _override_db(engine)}
    client = TestClient(app)
    try:
        response = client.post(
            "/api/order-fulfillment/bitrix/messages",
            json={
                "chat_code": service.CHAT_COURIER_SPB,
                "dialog_id": "chat727",
                "chat_id": 727,
                "message_id": 77,
                "ocr_payloads": [
                    {
                        "orders": ["218530"],
                        "delivery_status": "delivered",
                        "payment_collected": False,
                        "confidence": 0.91,
                    }
                ],
            },
            headers=_headers(),
        )
        recommendations = client.get(
            "/api/order-fulfillment/cases/recommendations",
            headers=_headers(),
        )
    finally:
        app.dependency_overrides = {}

    assert response.status_code == 200
    assert response.json()["events_created"] == 1
    assert recommendations.status_code == 200
    item = recommendations.json()["items"][0]
    assert item["derived_status"] == service.EVENT_COURIER_DELIVERED_PENDING
    assert item["recommended_stage"] == "IN_DELIVERY"
    assert item["recommended_stage"] != "WON"


def test_order_fulfillment_review_endpoint_requires_token(monkeypatch) -> None:
    _configure(monkeypatch)
    client = TestClient(app)

    response = client.get("/api/order-fulfillment/cases/review")

    assert response.status_code == 401


def test_order_fulfillment_review_endpoint_handles_missing_external_config(monkeypatch) -> None:
    _configure(monkeypatch)
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    for table in SITE_ORDER_TABLES:
        table.create(engine)
    app.dependency_overrides = {get_db: _override_db(engine)}
    client = TestClient(app)
    try:
        create_response = client.post(
            "/api/order-fulfillment/bitrix/messages",
            json={
                "chat_code": service.CHAT_SITE_MASTER_MOBILE,
                "dialog_id": "chat733",
                "chat_id": 733,
                "message_id": 88,
                "text": "218014 не забрали",
            },
            headers=_headers(),
        )
        review_response = client.get(
            "/api/order-fulfillment/cases/review",
            headers=_headers(),
        )
    finally:
        app.dependency_overrides = {}

    assert create_response.status_code == 200
    assert review_response.status_code == 200
    assert create_response.json()["events_created"] == 0
    assert review_response.json()["items"] == []
