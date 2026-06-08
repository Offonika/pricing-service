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


def test_order_fulfillment_persisted_message_recommendations(monkeypatch) -> None:
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
    item = review_response.json()["items"][0]
    assert item["site_order_number"] == "218014"
    assert item["recommended_stage"] == service.CRM_STAGE_MANUAL_REVIEW
    assert item["action"] == "manual_review"
    assert "bitrix_config_missing" in item["manual_review_reason"]
    assert "onec_config_missing" in item["manual_review_reason"]
