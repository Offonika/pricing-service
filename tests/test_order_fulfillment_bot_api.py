from __future__ import annotations

import json
from collections.abc import Generator
from datetime import datetime
from urllib.parse import urlencode

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.dependencies import get_db
from app.core.config import get_settings
from app.main import app
from app.models import Base
from app.models.logistics import LogisticsWarehouse
from app.models.site_order_fulfillment import (
    BitrixChatActionCandidate,
    SiteOrderFulfillmentOutbox,
)
from app.services import site_order_fulfillment_bot as bot


def _override_db(engine):
    def override() -> Generator[Session, None, None]:
        with Session(engine) as session:
            yield session

    return override


def _configure(monkeypatch) -> None:
    monkeypatch.setenv("ORDER_FULFILLMENT_BOT_ENABLED", "true")
    monkeypatch.setenv("ORDER_FULFILLMENT_BOT_APPLY_ENABLED", "false")
    monkeypatch.setenv("ORDER_FULFILLMENT_BOT_APPLICATION_TOKEN", "app-token")
    monkeypatch.setenv("ORDER_FULFILLMENT_BOT_CALLBACK_SECRET", "callback-secret")
    monkeypatch.setenv("ORDER_FULFILLMENT_BOT_ALLOWED_DOMAINS", "crm.example")
    monkeypatch.setenv("ORDER_FULFILLMENT_BOT_ALLOWED_MEMBER_IDS", "member-1")
    monkeypatch.setenv("ORDER_FULFILLMENT_BOT_SOURCE_CHAT_IDS", "chat8729,chat733")
    monkeypatch.setenv("ORDER_FULFILLMENT_INTERNAL_API_TOKEN", "internal-token")
    get_settings.cache_clear()


def _engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            LogisticsWarehouse(
                external_id="mitino",
                name="Митино магазин",
                kind="retail",
                is_active=True,
                payload={"aliases": ["Митино"]},
            )
        )
        session.commit()
    return engine


def _message_form(*, token: str = "app-token") -> dict[str, str]:
    return {
        "event": "ONIMBOTMESSAGEADD",
        "auth[application_token]": token,
        "auth[domain]": "crm.example",
        "auth[member_id]": "member-1",
        "data[PARAMS][DIALOG_ID]": "chat8729",
        "data[PARAMS][MESSAGE_ID]": "1001",
        "data[PARAMS][FROM_USER_ID]": "7",
        "data[PARAMS][MESSAGE]": "Заказ 241500 прибыл в Митино",
        "data[PARAMS][DATE_CREATE]": "2026-08-23T12:00:00+03:00",
    }


def _mark_card_ready(session: Session, candidate: BitrixChatActionCandidate) -> None:
    publish_row = session.scalar(
        select(SiteOrderFulfillmentOutbox).where(
            SiteOrderFulfillmentOutbox.idempotency_key == f"candidate:{candidate.id}:publish"
        )
    )
    assert publish_row is not None
    publish_row.status = bot.OUTBOX_COMPLETED
    candidate.bot_message_id = "9001"
    session.commit()


def test_bot_event_rejects_invalid_application_token(monkeypatch) -> None:
    _configure(monkeypatch)
    client = TestClient(app)

    response = client.post(
        "/api/order-fulfillment/bitrix-bot/events",
        data=_message_form(token="wrong"),
    )

    assert response.status_code == 403


def test_authenticated_service_event_without_dialog_is_ignored(monkeypatch) -> None:
    _configure(monkeypatch)

    response = TestClient(app).post(
        "/api/order-fulfillment/bitrix-bot/events",
        data={
            "event": "ONIMBOTDELETE",
            "auth[application_token]": "app-token",
            "auth[domain]": "crm.example",
            "auth[member_id]": "member-1",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"accepted": True, "ignored": True}


def test_authenticated_callback_without_event_name_is_rejected(monkeypatch) -> None:
    _configure(monkeypatch)

    response = TestClient(app).post(
        "/api/order-fulfillment/bitrix-bot/events",
        data={
            "auth[application_token]": "app-token",
            "auth[domain]": "crm.example",
            "auth[member_id]": "member-1",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "event name is missing"


def test_message_event_runtime_switch_forces_dry_run_candidate(monkeypatch) -> None:
    _configure(monkeypatch)
    monkeypatch.setenv("ORDER_FULFILLMENT_BOT_APPLY_ENABLED", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(
        bot,
        "runtime_apply_enabled_from_env",
        lambda **_: False,
    )
    engine = _engine()
    app.dependency_overrides = {get_db: _override_db(engine)}
    client = TestClient(app)
    try:
        response = client.post(
            "/api/order-fulfillment/bitrix-bot/events",
            data=_message_form(),
        )
        with Session(engine) as session:
            candidate = session.scalar(select(BitrixChatActionCandidate))
    finally:
        app.dependency_overrides = {}
        engine.dispose()
        get_settings.cache_clear()

    assert response.status_code == 200
    assert response.json() == {"accepted": True, "candidates": 1}
    assert candidate is not None
    assert candidate.dry_run is True


def test_message_event_rejects_non_numeric_message_id(monkeypatch) -> None:
    _configure(monkeypatch)
    engine = _engine()
    app.dependency_overrides = {get_db: _override_db(engine)}
    client = TestClient(app)
    form = _message_form()
    form["data[PARAMS][MESSAGE_ID]"] = "invalid"
    try:
        response = client.post(
            "/api/order-fulfillment/bitrix-bot/events",
            data=form,
        )
    finally:
        app.dependency_overrides = {}
        engine.dispose()

    assert response.status_code == 400
    assert response.json()["detail"] == "message id must be numeric"


def test_unrecognized_message_still_rejects_non_numeric_message_id(monkeypatch) -> None:
    _configure(monkeypatch)
    engine = _engine()
    app.dependency_overrides = {get_db: _override_db(engine)}
    client = TestClient(app)
    form = _message_form()
    form["data[PARAMS][MESSAGE_ID]"] = "invalid"
    form["data[PARAMS][MESSAGE]"] = "Обычное сообщение без заказа"
    try:
        response = client.post(
            "/api/order-fulfillment/bitrix-bot/events",
            data=form,
        )
    finally:
        app.dependency_overrides = {}
        engine.dispose()

    assert response.status_code == 400
    assert response.json()["detail"] == "message id must be numeric"


def test_command_event_queues_signed_action(monkeypatch) -> None:
    _configure(monkeypatch)
    engine = _engine()
    app.dependency_overrides = {get_db: _override_db(engine)}
    client = TestClient(app)
    try:
        assert (
            client.post(
                "/api/order-fulfillment/bitrix-bot/events",
                data=_message_form(),
            ).status_code
            == 200
        )
        with Session(engine) as session:
            candidate = session.scalar(select(BitrixChatActionCandidate))
            assert candidate is not None
            _mark_card_ready(session, candidate)
            token = bot.sign_callback_token(
                candidate,
                action=bot.ACTION_ARRIVED,
                step=1,
                secret="callback-secret",
            )
        response = client.post(
            "/api/order-fulfillment/bitrix-bot/events",
            data={
                "event": "ONIMCOMMANDADD",
                "auth[application_token]": "app-token",
                "auth[domain]": "crm.example",
                "auth[member_id]": "member-1",
                "data[PARAMS][DIALOG_ID]": "chat8729",
                "data[COMMAND]": "pickup_action",
                "data[COMMAND_PARAMS]": token,
                "data[USER][ID]": "7",
            },
        )
    finally:
        app.dependency_overrides = {}
        engine.dispose()

    assert response.status_code == 200
    assert response.json()["accepted"] is True
    assert response.json()["duplicate"] is False


def test_command_event_reads_real_dynamic_command_payload(monkeypatch) -> None:
    _configure(monkeypatch)
    monkeypatch.setenv("ORDER_FULFILLMENT_BOT_COMMAND_ID", "103")
    get_settings.cache_clear()
    engine = _engine()
    app.dependency_overrides = {get_db: _override_db(engine)}
    client = TestClient(app)
    try:
        assert (
            client.post(
                "/api/order-fulfillment/bitrix-bot/events",
                data=_message_form(),
            ).status_code
            == 200
        )
        with Session(engine) as session:
            candidate = session.scalar(select(BitrixChatActionCandidate))
            assert candidate is not None
            _mark_card_ready(session, candidate)
            token = bot.sign_callback_token(
                candidate,
                action=bot.ACTION_ARRIVED,
                step=1,
                secret="callback-secret",
            )
        response = client.post(
            "/api/order-fulfillment/bitrix-bot/events",
            data={
                "event": "ONIMCOMMANDADD",
                "auth[application_token]": "app-token",
                "auth[domain]": "crm.example",
                "auth[member_id]": "member-1",
                "data[PARAMS][FROM_USER_ID]": "7",
                "data[COMMAND][103][DIALOG_ID]": "chat8729",
                "data[COMMAND][103][COMMAND]": "pickup_action",
                "data[COMMAND][103][COMMAND_ID]": "103",
                "data[COMMAND][103][COMMAND_PARAMS]": token,
            },
        )
    finally:
        app.dependency_overrides = {}
        engine.dispose()

    assert response.status_code == 200
    assert response.json()["accepted"] is True


def test_command_event_rejects_unexpected_command_id(monkeypatch) -> None:
    _configure(monkeypatch)
    monkeypatch.setenv("ORDER_FULFILLMENT_BOT_COMMAND_ID", "103")
    get_settings.cache_clear()
    response = TestClient(app).post(
        "/api/order-fulfillment/bitrix-bot/events",
        data={
            "event": "ONIMCOMMANDADD",
            "auth[application_token]": "app-token",
            "auth[domain]": "crm.example",
            "auth[member_id]": "member-1",
            "data[PARAMS][DIALOG_ID]": "chat8729",
            "data[COMMAND][999][COMMAND]": "pickup_action",
            "data[COMMAND][999][COMMAND_PARAMS]": "invalid",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "unexpected command id"


def test_command_event_rejects_missing_command_name(monkeypatch) -> None:
    _configure(monkeypatch)

    response = TestClient(app).post(
        "/api/order-fulfillment/bitrix-bot/events",
        data={
            "event": "ONIMCOMMANDADD",
            "auth[application_token]": "app-token",
            "auth[domain]": "crm.example",
            "auth[member_id]": "member-1",
            "data[PARAMS][DIALOG_ID]": "chat8729",
            "data[COMMAND_PARAMS]": "signed-token",
            "data[USER][ID]": "7",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "command is missing"


def test_bot_event_rejects_oversized_body_before_parsing(monkeypatch) -> None:
    _configure(monkeypatch)
    monkeypatch.setenv("ORDER_FULFILLMENT_BOT_CALLBACK_MAX_BODY_BYTES", "1024")
    get_settings.cache_clear()

    response = TestClient(app).post(
        "/api/order-fulfillment/bitrix-bot/events",
        content=b"x" * 1025,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 413
    assert response.json()["detail"] == "request body is too large"


def test_bot_event_rejects_deeply_nested_json_without_recursion_error(monkeypatch) -> None:
    _configure(monkeypatch)
    nested = '{"value":' + "[" * 1200 + "0" + "]" * 1200 + "}"

    response = TestClient(app).post(
        "/api/order-fulfillment/bitrix-bot/events",
        content=urlencode({"data": nested}).encode(),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "callback JSON is too deeply nested"


def test_bot_event_rejects_json_beyond_configured_depth(monkeypatch) -> None:
    _configure(monkeypatch)
    nested = '{"value":' + "[" * 17 + "0" + "]" * 17 + "}"

    response = TestClient(app).post(
        "/api/order-fulfillment/bitrix-bot/events",
        content=urlencode({"data": nested}).encode(),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "callback JSON is too deeply nested"


def test_bot_event_rejects_json_with_too_many_scalar_values(monkeypatch) -> None:
    _configure(monkeypatch)
    nested = json.dumps({"values": list(range(513))})

    response = TestClient(app).post(
        "/api/order-fulfillment/bitrix-bot/events",
        content=urlencode({"data": nested}).encode(),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "callback JSON has too many values"


def test_bot_event_rejects_too_many_form_fields(monkeypatch) -> None:
    _configure(monkeypatch)
    fields = [(f"field_{index}", "x") for index in range(257)]

    response = TestClient(app).post(
        "/api/order-fulfillment/bitrix-bot/events",
        content=urlencode(fields).encode(),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "callback has too many form fields"


def test_bot_health_requires_internal_bearer(monkeypatch) -> None:
    _configure(monkeypatch)
    response = TestClient(app).get("/api/order-fulfillment/bitrix-bot/internal/health")

    assert response.status_code == 401


def test_bot_health_reports_runtime_apply_switch(monkeypatch) -> None:
    _configure(monkeypatch)
    monkeypatch.setenv("ORDER_FULFILLMENT_BOT_APPLY_ENABLED", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(
        bot,
        "runtime_apply_enabled_from_env",
        lambda **_: False,
    )
    monkeypatch.setattr(bot, "utcnow", lambda: datetime(2026, 8, 24, 12, 0))
    engine = _engine()
    with Session(engine) as session:
        pending = bot.enqueue_outbox(
            session,
            operation=bot.OP_CREATE_TASK,
            idempotency_key="health-pending",
            target_type="deal",
            target_id="500",
            payload={},
            now=datetime(2026, 8, 24, 10, 0),
        )
        pending.created_at = datetime(2026, 8, 24, 10, 0)
        processing = bot.enqueue_outbox(
            session,
            operation=bot.OP_UPDATE_CRM_STAGE,
            idempotency_key="health-processing",
            target_type="deal",
            target_id="500",
            payload={},
            now=datetime(2026, 8, 24, 11, 0),
        )
        processing.status = bot.OUTBOX_PROCESSING
        processing.created_at = datetime(2026, 8, 24, 11, 0)
        session.commit()
    app.dependency_overrides = {get_db: _override_db(engine)}
    try:
        response = TestClient(app).get(
            "/api/order-fulfillment/bitrix-bot/internal/health",
            headers={"Authorization": "Bearer internal-token"},
        )
    finally:
        app.dependency_overrides = {}
        engine.dispose()
        get_settings.cache_clear()

    assert response.status_code == 200
    assert response.json()["apply_configured_at_startup"] is True
    assert response.json()["apply_enabled"] is False
    assert response.json()["outbox_pending"] == 1
    assert response.json()["outbox_processing"] == 1
    assert response.json()["outbox_blocked_by_apply"] == 2
    assert response.json()["oldest_active_outbox_age_seconds"] == 7200
