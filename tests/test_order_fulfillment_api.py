from __future__ import annotations

from collections.abc import Generator

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.dependencies import get_db
from app.core.config import get_settings
from app.main import app
from app.models.site_order_fulfillment import (
    BitrixChatMention,
    BitrixChatMessage,
    SiteOrderExecutionCase,
    SiteOrderExecutionEvent,
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


def test_shipment_sync_creates_only_explicit_missing_part_via_gateway(
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

    class Gateway:
        def __init__(self, **kwargs):
            captured["gateway_config"] = kwargs

        def ensure_shipment(self, **kwargs):
            captured["ensure"] = kwargs
            return {
                "ok": True,
                "shipment": {
                    "shipment_id": 52,
                    "items": [{"basket_item_id": 702, "shipment_item_id": 802}],
                },
            }

        def list_shipments(self, **kwargs):
            captured["list"] = kwargs
            return [{"shipment_id": 51, "tracking_number": ""}]

    def fake_sync(db, **kwargs):
        del db
        captured["sync"] = kwargs
        return shipment_service.ShipmentSyncResult(
            site_order_number=kwargs["site_order_number"],
            coverage_status="complete",
            full_assembly=True,
            shipment_count=2,
            target_stage="FINAL_INVOICE",
            action="update_stage",
            reason="all_order_quantities_assembled",
        )

    monkeypatch.setattr(shipment_service, "BitrixSaleShipmentGatewayClient", Gateway)
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
    assert captured["ensure"]["shipment_key"] == "part-2"
    assert captured["sync"]["shipments"][0]["bitrix_shipment_id"] == 51
    assert captured["sync"]["shipments"][1]["bitrix_shipment_id"] == 52


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
