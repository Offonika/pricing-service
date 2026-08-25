from __future__ import annotations

import json
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api import order_fulfillment_bot as bot_api
from app.api.dependencies import get_db
from app.core.config import get_settings
from app.main import app
from app.models import Base
from app.models.logistics import LogisticsWarehouse
from app.models.site_order_fulfillment import (
    BitrixBotInputSession,
    BitrixChatActionCandidate,
    BitrixChatMessage,
    SiteOrderFulfillmentOutbox,
)
from app.services import pickup_inventory
from app.services import site_order_fulfillment as fulfillment
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
    monkeypatch.setenv("ORDER_FULFILLMENT_BOT_SEARCH_COMMAND_ID", "201")
    monkeypatch.setenv("ORDER_FULFILLMENT_BOT_ARRIVAL_COMMAND_ID", "202")
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


def _public_command_form(
    *,
    command: str,
    command_id: str,
    params: str,
    message_id: str = "1100",
) -> dict[str, str]:
    prefix = f"data[COMMAND][{command_id}]"
    return {
        "event": "ONIMCOMMANDADD",
        "auth[application_token]": "app-token",
        "auth[domain]": "crm.example",
        "auth[member_id]": "member-1",
        f"{prefix}[COMMAND]": command,
        f"{prefix}[DIALOG_ID]": "chat8729",
        f"{prefix}[MESSAGE_ID]": message_id,
        f"{prefix}[USER_ID]": "7",
        f"{prefix}[COMMAND_PARAMS]": params,
    }


def test_public_pickup_search_is_read_only_and_queues_one_card(monkeypatch) -> None:
    _configure(monkeypatch)
    engine = _engine()
    app.dependency_overrides = {get_db: _override_db(engine)}
    client = TestClient(app)
    try:
        response = client.post(
            "/api/order-fulfillment/bitrix-bot/events",
            data=_public_command_form(command="pickup", command_id="201", params="241500"),
        )
        with Session(engine) as session:
            candidate = session.scalar(select(BitrixChatActionCandidate))
            rows = session.scalars(select(SiteOrderFulfillmentOutbox)).all()
    finally:
        app.dependency_overrides = {}
        engine.dispose()
        get_settings.cache_clear()

    assert response.status_code == 200
    assert response.json()["interaction"] == "search"
    assert candidate is not None
    assert candidate.payload["interaction"] == "search"
    assert [row.operation for row in rows] == [bot.OP_PUBLISH_CARD]


def test_russian_menu_search_message_uses_read_only_interaction(monkeypatch) -> None:
    _configure(monkeypatch)
    engine = _engine()
    app.dependency_overrides = {get_db: _override_db(engine)}
    client = TestClient(app)
    form = _message_form()
    form["data[PARAMS][MESSAGE]"] = "Найти заказ 241500"
    try:
        response = client.post("/api/order-fulfillment/bitrix-bot/events", data=form)
        with Session(engine) as session:
            candidate = session.scalar(select(BitrixChatActionCandidate))
            rows = session.scalars(select(SiteOrderFulfillmentOutbox)).all()
    finally:
        app.dependency_overrides = {}
        engine.dispose()
        get_settings.cache_clear()

    assert response.status_code == 200
    assert response.json()["interaction"] == "search"
    assert candidate is not None
    assert candidate.payload["interaction"] == "search"
    assert [row.operation for row in rows] == [bot.OP_PUBLISH_CARD]


def test_russian_menu_arrival_accepts_multiple_orders(monkeypatch) -> None:
    _configure(monkeypatch)
    engine = _engine()
    app.dependency_overrides = {get_db: _override_db(engine)}
    client = TestClient(app)
    form = _message_form()
    form["data[PARAMS][MESSAGE]"] = "Зафиксировать поступление 241500 241501"
    try:
        response = client.post("/api/order-fulfillment/bitrix-bot/events", data=form)
        with Session(engine) as session:
            candidate = session.scalar(select(BitrixChatActionCandidate))
    finally:
        app.dependency_overrides = {}
        engine.dispose()
        get_settings.cache_clear()

    assert response.status_code == 200
    assert response.json()["interaction"] == "structured_arrival"
    assert candidate is not None
    assert candidate.payload["order_numbers"] == ["241500", "241501"]


def test_russian_menu_prefix_is_strict() -> None:
    assert bot_api._russian_menu_interaction("Найти заказ 241500") == (
        "search",
        "241500",
    )
    assert bot_api._russian_menu_interaction("найдите, пожалуйста, заказ 241500") is None
    assert bot_api._russian_menu_interaction("Зафиксировать поступление") is None


def test_russian_menu_button_starts_actor_scoped_input_session(monkeypatch) -> None:
    _configure(monkeypatch)
    engine = _engine()
    app.dependency_overrides = {get_db: _override_db(engine)}
    client = TestClient(app)
    form = _message_form()
    form["data[PARAMS][MESSAGE]"] = "Зафиксировать поступление"
    try:
        response = client.post("/api/order-fulfillment/bitrix-bot/events", data=form)
        with Session(engine) as session:
            input_session = session.scalar(select(BitrixBotInputSession))
            candidates = session.scalars(select(BitrixChatActionCandidate)).all()
            rows = session.scalars(select(SiteOrderFulfillmentOutbox)).all()
    finally:
        app.dependency_overrides = {}
        engine.dispose()
        get_settings.cache_clear()

    assert response.status_code == 200
    assert response.json()["awaiting_input"] is True
    assert input_session is not None
    assert input_session.actor_id == "7"
    assert input_session.interaction == "structured_arrival"
    assert input_session.status == "pending"
    assert candidates == []
    assert [row.operation for row in rows] == [bot.OP_PUBLISH_MENU_INPUT_PROMPT]


def test_russian_menu_consumes_only_same_actor_next_message(monkeypatch) -> None:
    _configure(monkeypatch)
    engine = _engine()
    app.dependency_overrides = {get_db: _override_db(engine)}
    client = TestClient(app)
    start = _message_form()
    start["data[PARAMS][MESSAGE]"] = "Зафиксировать поступление"
    other = _message_form()
    other["data[PARAMS][MESSAGE_ID]"] = "1002"
    other["data[PARAMS][FROM_USER_ID]"] = "8"
    other["data[PARAMS][MESSAGE]"] = "241599"
    answer = _message_form()
    answer["data[PARAMS][MESSAGE_ID]"] = "1003"
    answer["data[PARAMS][MESSAGE]"] = "241500 241501"
    try:
        assert (
            client.post("/api/order-fulfillment/bitrix-bot/events", data=start).status_code == 200
        )
        other_response = client.post("/api/order-fulfillment/bitrix-bot/events", data=other)
        answer_response = client.post("/api/order-fulfillment/bitrix-bot/events", data=answer)
        with Session(engine) as session:
            input_session = session.scalar(select(BitrixBotInputSession))
            candidates = session.scalars(
                select(BitrixChatActionCandidate).order_by(BitrixChatActionCandidate.id)
            ).all()
    finally:
        app.dependency_overrides = {}
        engine.dispose()
        get_settings.cache_clear()

    assert other_response.status_code == 200
    assert other_response.json()["ignored"] is True
    assert answer_response.status_code == 200
    assert answer_response.json()["orders"] == 2
    assert input_session is not None
    assert input_session.status == "consumed"
    assert input_session.consumed_message_id == "1003"
    assert [candidate.site_order_number for candidate in candidates] == ["241500", "241501"]


def test_russian_menu_invalid_input_keeps_session_and_queues_hint(monkeypatch) -> None:
    _configure(monkeypatch)
    engine = _engine()
    app.dependency_overrides = {get_db: _override_db(engine)}
    client = TestClient(app)
    start = _message_form()
    start["data[PARAMS][MESSAGE]"] = "Найти заказ"
    invalid = _message_form()
    invalid["data[PARAMS][MESSAGE_ID]"] = "1002"
    invalid["data[PARAMS][MESSAGE]"] = "посмотрите заказ выше"
    try:
        client.post("/api/order-fulfillment/bitrix-bot/events", data=start)
        response = client.post("/api/order-fulfillment/bitrix-bot/events", data=invalid)
        with Session(engine) as session:
            input_session = session.scalar(select(BitrixBotInputSession))
            rows = session.scalars(
                select(SiteOrderFulfillmentOutbox).order_by(SiteOrderFulfillmentOutbox.id)
            ).all()
    finally:
        app.dependency_overrides = {}
        engine.dispose()
        get_settings.cache_clear()

    assert response.status_code == 200
    assert response.json()["invalid_input"] is True
    assert input_session is not None and input_session.status == "pending"
    assert [row.payload["prompt_kind"] for row in rows] == ["start", "invalid"]


def test_russian_menu_repeat_start_supersedes_previous_session(monkeypatch) -> None:
    _configure(monkeypatch)
    engine = _engine()
    app.dependency_overrides = {get_db: _override_db(engine)}
    client = TestClient(app)
    first = _message_form()
    first["data[PARAMS][MESSAGE]"] = "Найти заказ"
    second = _message_form()
    second["data[PARAMS][MESSAGE_ID]"] = "1002"
    second["data[PARAMS][MESSAGE]"] = "Зафиксировать поступление"
    try:
        client.post("/api/order-fulfillment/bitrix-bot/events", data=first)
        client.post("/api/order-fulfillment/bitrix-bot/events", data=second)
        with Session(engine) as session:
            sessions = session.scalars(
                select(BitrixBotInputSession).order_by(BitrixBotInputSession.id)
            ).all()
    finally:
        app.dependency_overrides = {}
        engine.dispose()
        get_settings.cache_clear()

    assert [item.status for item in sessions] == ["superseded", "pending"]
    assert sessions[1].interaction == "structured_arrival"


def test_russian_menu_expired_session_does_not_capture_message(monkeypatch) -> None:
    _configure(monkeypatch)
    engine = _engine()
    app.dependency_overrides = {get_db: _override_db(engine)}
    client = TestClient(app)
    start = _message_form()
    start["data[PARAMS][MESSAGE]"] = "Найти заказ"
    answer = _message_form()
    answer["data[PARAMS][MESSAGE_ID]"] = "1002"
    answer["data[PARAMS][MESSAGE]"] = "241500"
    try:
        client.post("/api/order-fulfillment/bitrix-bot/events", data=start)
        with Session(engine) as session:
            input_session = session.scalar(select(BitrixBotInputSession))
            assert input_session is not None
            input_session.expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=1)
            session.commit()
        response = client.post("/api/order-fulfillment/bitrix-bot/events", data=answer)
        with Session(engine) as session:
            input_session = session.scalar(select(BitrixBotInputSession))
            candidates = session.scalars(select(BitrixChatActionCandidate)).all()
    finally:
        app.dependency_overrides = {}
        engine.dispose()
        get_settings.cache_clear()

    assert response.status_code == 200, response.json()
    assert response.json()["ignored"] is True
    assert input_session is not None and input_session.status == "expired"
    assert candidates == []


def test_russian_menu_consumed_message_replay_is_idempotent(monkeypatch) -> None:
    _configure(monkeypatch)
    engine = _engine()
    app.dependency_overrides = {get_db: _override_db(engine)}
    client = TestClient(app)
    start = _message_form()
    start["data[PARAMS][MESSAGE]"] = "Найти заказ"
    answer = _message_form()
    answer["data[PARAMS][MESSAGE_ID]"] = "1002"
    answer["data[PARAMS][MESSAGE]"] = "241500"
    try:
        client.post("/api/order-fulfillment/bitrix-bot/events", data=start)
        first = client.post("/api/order-fulfillment/bitrix-bot/events", data=answer)
        replay = client.post("/api/order-fulfillment/bitrix-bot/events", data=answer)
        with Session(engine) as session:
            candidates = session.scalars(select(BitrixChatActionCandidate)).all()
            rows = session.scalars(select(SiteOrderFulfillmentOutbox)).all()
    finally:
        app.dependency_overrides = {}
        engine.dispose()
        get_settings.cache_clear()

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json()["duplicate"] is True
    assert len(candidates) == 1
    assert len(rows) == 2


def test_public_structured_arrival_groups_multiple_orders_in_one_card(monkeypatch) -> None:
    _configure(monkeypatch)
    engine = _engine()
    app.dependency_overrides = {get_db: _override_db(engine)}
    client = TestClient(app)
    try:
        response = client.post(
            "/api/order-fulfillment/bitrix-bot/events",
            data=_public_command_form(
                command="pickup_arrival",
                command_id="202",
                params="241500, 241501",
            ),
        )
        with Session(engine) as session:
            candidates = session.scalars(
                select(BitrixChatActionCandidate).order_by(BitrixChatActionCandidate.id)
            ).all()
            rows = session.scalars(select(SiteOrderFulfillmentOutbox)).all()
    finally:
        app.dependency_overrides = {}
        engine.dispose()
        get_settings.cache_clear()

    assert response.status_code == 200
    assert response.json()["orders"] == 2
    assert len(candidates) == 2
    assert candidates[0].payload["order_numbers"] == ["241500", "241501"]
    assert len(rows) == 1 and rows[0].candidate_id == candidates[0].id


def test_public_command_rejects_discursive_or_phone_like_input(monkeypatch) -> None:
    _configure(monkeypatch)
    engine = _engine()
    app.dependency_overrides = {get_db: _override_db(engine)}
    client = TestClient(app)
    try:
        response = client.post(
            "/api/order-fulfillment/bitrix-bot/events",
            data=_public_command_form(
                command="pickup",
                command_id="201",
                params="проверь 241500 телефон 9991234567",
            ),
        )
    finally:
        app.dependency_overrides = {}
        engine.dispose()
        get_settings.cache_clear()

    assert response.status_code == 400
    assert response.json()["detail"] == "order_number_invalid"


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


def test_command_event_routes_inventory_clarification_token(monkeypatch) -> None:
    _configure(monkeypatch)
    monkeypatch.setenv(
        "ORDER_FULFILLMENT_BOT_SOURCE_CHAT_IDS",
        "chat8729,chat733,chat8961",
    )
    get_settings.cache_clear()
    engine = _engine()
    app.dependency_overrides = {get_db: _override_db(engine)}
    client = TestClient(app)
    prompt_now = datetime.now(UTC).replace(tzinfo=None)
    try:
        with Session(engine) as session:
            message = BitrixChatMessage(
                chat_code=fulfillment.CHAT_PICKUP_INVENTORY,
                dialog_id="chat8961",
                chat_id=8961,
                message_id=2001,
                message_at=datetime(2026, 8, 24, 10),
                author_id="7",
                raw_text_hash=fulfillment._text_hash("Полный список 241500"),  # noqa: SLF001
                raw_text_redacted="Полный список <order>",
                parser_version="test",
                parse_status="inventory_manual_review",
                payload={"text": "Полный список 241500"},
            )
            session.add(message)
            session.flush()
            submission = pickup_inventory.persist_inventory_message(
                session,
                message=message,
            )
            assert submission is not None
            bot.enqueue_inventory_clarification_card(
                session,
                submission=submission,
                settings=get_settings(),
                now=prompt_now,
            )
            state = dict((submission.payload or {})["clarification"])
            state["bot_message_id"] = "9100"
            submission.payload = {**(submission.payload or {}), "clarification": state}
            publish = session.scalar(
                select(SiteOrderFulfillmentOutbox).where(
                    SiteOrderFulfillmentOutbox.idempotency_key
                    == f"inventory-submission:{submission.id}:publish"
                )
            )
            assert publish is not None
            publish.status = bot.OUTBOX_COMPLETED
            session.commit()
            session.refresh(submission)
            token = bot.sign_inventory_callback_token(
                submission,
                action=bot.INVENTORY_ACTION_SELECT_POINT,
                secret="callback-secret",
                warehouse_external_id="mitino",
            )
        response = client.post(
            "/api/order-fulfillment/bitrix-bot/events",
            data={
                "event": "ONIMCOMMANDADD",
                "auth[application_token]": "app-token",
                "auth[domain]": "crm.example",
                "auth[member_id]": "member-1",
                "data[PARAMS][DIALOG_ID]": "chat8961",
                "data[COMMAND]": "pickup_action",
                "data[COMMAND_PARAMS]": token,
                "data[USER][ID]": "7",
            },
        )
        with Session(engine) as session:
            operation = session.scalar(
                select(SiteOrderFulfillmentOutbox).where(
                    SiteOrderFulfillmentOutbox.operation == bot.OP_PROCESS_INVENTORY_CLARIFICATION
                )
            )
    finally:
        app.dependency_overrides = {}
        engine.dispose()
        get_settings.cache_clear()

    assert response.status_code == 200, response.json()
    assert response.json()["inventory_clarification"] is True
    assert operation is not None


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
    monkeypatch.setenv("ORDER_FULFILLMENT_PICKUP_WAREHOUSE_EXTERNAL_IDS", "mitino")
    monkeypatch.setenv(
        "ORDER_FULFILLMENT_PICKUP_WAREHOUSE_ALIASES",
        '{"mitino":["Митино"]}',
    )
    monkeypatch.setenv(
        "ORDER_FULFILLMENT_POINT_TASK_ROUTES",
        '{"mitino":{"operator":200,"senior":201}}',
    )
    monkeypatch.setenv("ORDER_FULFILLMENT_INTERNET_SHOP_TASK_RESPONSIBLE_ID", "100")
    monkeypatch.setenv("ORDER_FULFILLMENT_SITE_RETURN_TASK_RESPONSIBLE_ID", "101")
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
    assert response.json()["auto_arrival_enabled"] is False
    assert response.json()["sms_workflow_configured"] is False
    assert response.json()["sms_workflow_template_id"] is None
    assert response.json()["outbox_pending"] == 1
    assert response.json()["outbox_processing"] == 1
    assert response.json()["outbox_blocked_by_apply"] == 2
    assert response.json()["oldest_active_outbox_age_seconds"] == 7200
    assert response.json()["pickup_warehouse_allowlist"] == ["mitino"]
    assert response.json()["pickup_warehouse_alias_count"] == 1
    assert response.json()["task_route_count"] == 1
    assert response.json()["task_route_configuration_errors"] == []
