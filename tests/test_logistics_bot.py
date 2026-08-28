from __future__ import annotations

import os
import tempfile
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.models import (
    Base,
    LogisticsBotSession,
    LogisticsBotSessionPhoto,
    LogisticsDraft,
    LogisticsTransfer,
    LogisticsTransferEvent,
    LogisticsUser,
)
from app.services import logistics as logistics_service
from app.telegram.logistics_bot import LogisticsTelegramBot, _remove_scan_picker


class FakeBotApi:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str, dict | None]] = []
        self.edits: list[tuple[int, int, str, dict | None]] = []
        self.callback_answers: list[tuple[str, str | None]] = []
        self._next_message_id = 1
        self.fail_edit_status_codes: list[int] = []

    def send_message(self, chat_id: int, text: str, reply_markup: dict | None = None) -> dict:
        self.messages.append((chat_id, text, reply_markup))
        result = {"message_id": self._next_message_id}
        self._next_message_id += 1
        return result

    def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict | None = None,
    ) -> dict:
        if self.fail_edit_status_codes:
            status_code = self.fail_edit_status_codes.pop(0)
            request = httpx.Request(
                "POST",
                "https://api.telegram.org/botTOKEN/editMessageText",
            )
            response = httpx.Response(
                status_code,
                request=request,
                json={"ok": False, "error_code": status_code},
            )
            raise httpx.HTTPStatusError(
                "telegram edit failed",
                request=request,
                response=response,
            )
        self.edits.append((chat_id, message_id, text, reply_markup))
        return {"message_id": message_id}

    def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> None:
        self.callback_answers.append((callback_query_id, text))


def setup_db():
    fd, path = tempfile.mkstemp(prefix="logistics_bot_", suffix=".db")
    os.close(fd)
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    return engine, path


def seed_data(engine) -> dict[str, int]:
    with Session(engine) as session:
        logistics_service.sync_warehouses(
            session,
            [
                {"external_id": "store-1", "name": "Магазин 1", "kind": "store"},
                {"external_id": "central", "name": "ЦС", "kind": "central"},
            ],
        )
        logistics_service.sync_drivers(
            session,
            [{"external_id": "driver-1", "full_name": "Иван Водитель"}],
        )
        logistics_service.sync_users(
            session,
            [
                {
                    "external_id": "sender",
                    "telegram_user_id": 101,
                    "username": "sender_user",
                    "full_name": "Отправитель",
                    "role": "sender",
                    "default_warehouse_external_id": "store-1",
                },
                {
                    "external_id": "receiver",
                    "telegram_user_id": 202,
                    "username": "receiver_user",
                    "full_name": "Получатель",
                    "role": "receiver",
                    "default_warehouse_external_id": "central",
                },
            ],
        )
        logistics_service.sync_transfers(
            session,
            [
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
            ],
        )
        sender = session.scalar(select(LogisticsUser).where(LogisticsUser.telegram_user_id == 101))
        receiver = session.scalar(
            select(LogisticsUser).where(LogisticsUser.telegram_user_id == 202)
        )
        transfer = session.scalar(select(LogisticsTransfer))
        return {
            "sender_id": sender.id,
            "receiver_id": receiver.id,
            "transfer_id": transfer.id,
        }


def _message(user_id: int, username: str, text: str | None = None, photo: list[dict] | None = None):
    payload = {
        "message": {
            "chat": {"id": user_id},
            "from": {"id": user_id, "username": username},
        }
    }
    if text is not None:
        payload["message"]["text"] = text
    if photo is not None:
        payload["message"]["photo"] = photo
    return payload


def _all_texts(fake_api: FakeBotApi) -> list[str]:
    return [text for _, text, _ in fake_api.messages] + [text for _, _, text, _ in fake_api.edits]


def test_logistics_telegram_bot_flow() -> None:
    engine, path = setup_db()
    ids = seed_data(engine)
    fake_api = FakeBotApi()
    bot = LogisticsTelegramBot(fake_api, engine)

    bot.handle_update(_message(101, "sender_user", "/start"))
    bot.handle_update(_message(101, "sender_user", "/handoff 1 2"))
    bot.handle_update(_message(101, "sender_user", "BC-0001"))
    bot.handle_update(
        {
            "message": {
                "chat": {"id": 101},
                "from": {"id": 101, "username": "sender_user"},
                "photo": [{"file_id": "photo-small"}, {"file_id": "photo-big"}],
                "caption": "П" * 1100,
            }
        }
    )
    bot.handle_update(_message(101, "sender_user", "/confirm"))

    bot.handle_update(_message(202, "receiver_user", "/receive"))
    bot.handle_update(_message(202, "receiver_user", "BC-0001"))
    bot.handle_update(_message(202, "receiver_user", "/confirm"))

    texts = _all_texts(fake_api)
    assert any("Создан черновик передачи" in text for text in texts)
    assert any("Фото добавлено" in text for text in texts)
    assert any("Черновик #1 подтвержден" in text for text in texts)
    assert any("Черновик #2 подтвержден" in text for text in texts)
    assert any("Последние сканы:" in text for text in texts)
    assert fake_api.edits

    with Session(engine) as session:
        history = logistics_service.get_transfer_history(session, transfer_id=ids["transfer_id"])
        assert history[1]["photos"][0]["telegram_file_id"] == "photo-big"
        assert history[1]["photos"][0]["comment"] == "П" * 1000
        assert history[0]["source"] == "telegram"
        assert history[1]["source"] == "telegram"
        assert session.query(LogisticsTransferEvent).count() == 2
        assert session.query(LogisticsBotSession).count() == 0
        assert session.query(LogisticsBotSessionPhoto).count() == 0

    engine.dispose()
    if os.path.exists(path):
        os.remove(path)


def test_logistics_telegram_bot_restores_session_after_restart() -> None:
    engine, path = setup_db()
    ids = seed_data(engine)

    fake_api_1 = FakeBotApi()
    bot_1 = LogisticsTelegramBot(fake_api_1, engine)
    bot_1.handle_update(_message(101, "sender_user", "/handoff 1 2"))
    bot_1.handle_update(
        {
            "message": {
                "chat": {"id": 101},
                "from": {"id": 101, "username": "sender_user"},
                "photo": [{"file_id": "photo-small"}, {"file_id": "photo-restart"}],
                "caption": "Фото до рестарта",
            }
        }
    )

    with Session(engine) as session:
        assert session.query(LogisticsBotSession).count() == 1
        assert session.query(LogisticsBotSessionPhoto).count() == 1

    fake_api_2 = FakeBotApi()
    bot_2 = LogisticsTelegramBot(fake_api_2, engine)
    bot_2.handle_update(_message(101, "sender_user", "BC-0001"))
    bot_2.handle_update(_message(101, "sender_user", "/confirm"))

    texts = _all_texts(fake_api_2)
    assert any("Штрихкод принят" in text for text in texts)
    assert any("подтвержден" in text for text in texts)
    assert fake_api_2.edits

    with Session(engine) as session:
        history = logistics_service.get_transfer_history(session, transfer_id=ids["transfer_id"])
        assert history[0]["photos"][0]["telegram_file_id"] == "photo-restart"
        assert history[0]["source"] == "telegram"
        assert session.query(LogisticsBotSession).count() == 0
        assert session.query(LogisticsBotSessionPhoto).count() == 0

    engine.dispose()
    if os.path.exists(path):
        os.remove(path)


@pytest.mark.parametrize("cancel_via_callback", [False, True])
def test_logistics_telegram_bot_cleans_stale_session_after_confirmed_draft(
    cancel_via_callback: bool,
) -> None:
    engine, path = setup_db()
    ids = seed_data(engine)
    fake_api = FakeBotApi()
    bot = LogisticsTelegramBot(fake_api, engine)

    bot.handle_update(_message(101, "sender_user", "/handoff 1 2"))
    bot.handle_update(_message(101, "sender_user", "BC-0001"))
    with Session(engine) as session:
        bot_session = session.query(LogisticsBotSession).one()
        logistics_service.confirm_draft(
            session,
            draft_id=bot_session.draft_id,
            actor_user_id=ids["sender_id"],
            comment=None,
            idempotency_key="test-confirm-before-session-cleanup",
            photos=[],
            source_channel="telegram",
        )

    if cancel_via_callback:
        bot.handle_update(
            {
                "callback_query": {
                    "id": "cb-stale-cancel",
                    "data": "draft:cancel",
                    "from": {"id": 101, "username": "sender_user"},
                    "message": {"chat": {"id": 101}},
                }
            }
        )
        assert fake_api.callback_answers[-1] == ("cb-stale-cancel", None)
    else:
        bot.handle_update(_message(101, "sender_user", "/cancel"))

    assert any("уже закрыт" in text for text in _all_texts(fake_api))
    with Session(engine) as session:
        assert session.query(LogisticsBotSession).count() == 0
        assert session.query(LogisticsBotSessionPhoto).count() == 0

    engine.dispose()
    if os.path.exists(path):
        os.remove(path)


def test_logistics_telegram_bot_does_not_carry_photos_into_replacement_draft() -> None:
    engine, path = setup_db()
    ids = seed_data(engine)
    fake_api = FakeBotApi()
    bot = LogisticsTelegramBot(fake_api, engine)

    bot.handle_update(_message(101, "sender_user", "/handoff 1 2"))
    bot.handle_update(_message(101, "sender_user", "BC-0001"))
    bot.handle_update(
        {
            "message": {
                "chat": {"id": 101},
                "from": {"id": 101, "username": "sender_user"},
                "photo": [{"file_id": "photo-from-closed-draft"}],
            }
        }
    )
    with Session(engine) as session:
        bot_session = session.query(LogisticsBotSession).one()
        closed_draft_id = bot_session.draft_id
        old_status_message_id = bot_session.status_message_id
        logistics_service.confirm_draft(
            session,
            draft_id=closed_draft_id,
            actor_user_id=ids["sender_id"],
            comment=None,
            idempotency_key="test-confirm-before-replacement-draft",
            photos=[],
            source_channel="telegram",
        )

    bot.handle_update(_message(101, "sender_user", "/handoff 1 2"))

    with Session(engine) as session:
        replacement_session = session.query(LogisticsBotSession).one()
        assert replacement_session.draft_id != closed_draft_id
        assert replacement_session.photos == []
        assert replacement_session.status_message_id != old_status_message_id
        assert session.query(LogisticsBotSessionPhoto).count() == 0

    engine.dispose()
    if os.path.exists(path):
        os.remove(path)


def test_logistics_telegram_bot_denies_draft_operations_for_logist() -> None:
    engine, path = setup_db()
    seed_data(engine)
    with Session(engine) as session:
        logistics_service.sync_users(
            session,
            [
                {
                    "external_id": "logist",
                    "telegram_user_id": 303,
                    "username": "logist_user",
                    "full_name": "Логист",
                    "role": "logist",
                    "default_warehouse_external_id": "store-1",
                }
            ],
        )
    fake_api = FakeBotApi()
    bot = LogisticsTelegramBot(fake_api, engine)

    bot.handle_update(_message(303, "logist_user", "/start"))
    menu = fake_api.messages[-1][2]
    callbacks = {button["callback_data"] for row in menu["inline_keyboard"] for button in row}
    assert "menu:handoff" not in callbacks
    assert "menu:receive" not in callbacks

    bot.handle_update(_message(303, "logist_user", "/handoff 1 2"))
    bot.handle_update(_message(303, "logist_user", "/receive"))
    bot.handle_update(
        {
            "callback_query": {
                "id": "cb-logist-handoff",
                "data": "menu:handoff",
                "from": {"id": 303, "username": "logist_user"},
                "message": {"chat": {"id": 303}},
            }
        }
    )

    texts = _all_texts(fake_api)
    assert any("только отправителю" in text for text in texts)
    assert any("только получателю" in text for text in texts)
    with Session(engine) as session:
        assert session.query(LogisticsDraft).count() == 0
        assert session.query(LogisticsBotSession).count() == 0

    engine.dispose()
    if os.path.exists(path):
        os.remove(path)


def test_remove_scan_picker_paginates_more_than_twenty_items() -> None:
    draft = SimpleNamespace(
        items=[
            SimpleNamespace(
                id=item_id,
                barcode=f"BC-{item_id:02d}",
                transfer=SimpleNamespace(document_number=f"РТУ-{item_id:02d}"),
            )
            for item_id in range(21, 0, -1)
        ]
    )

    first_text, first_markup = _remove_scan_picker(draft, 0)
    first_buttons = [button for row in first_markup["inline_keyboard"] for button in row]
    assert "Страница 1/2" in first_text
    assert any(button["callback_data"] == "draft:remove_picker:1" for button in first_buttons)
    assert not any(button["callback_data"] == "draft:remove:21" for button in first_buttons)

    second_text, second_markup = _remove_scan_picker(draft, 1)
    second_buttons = [button for row in second_markup["inline_keyboard"] for button in row]
    assert "Страница 2/2" in second_text
    assert any(button["callback_data"] == "draft:remove:21" for button in second_buttons)
    assert any(button["callback_data"] == "draft:remove_picker:0" for button in second_buttons)


def test_logistics_telegram_bot_falls_back_to_new_status_message_when_edit_fails() -> None:
    engine, path = setup_db()
    seed_data(engine)

    fake_api = FakeBotApi()
    bot = LogisticsTelegramBot(fake_api, engine)

    bot.handle_update(_message(101, "sender_user", "/handoff 1 2"))
    fake_api.fail_edit_status_codes.append(400)
    bot.handle_update(_message(101, "sender_user", "BC-0001"))

    with Session(engine) as session:
        bot_session = session.scalar(select(LogisticsBotSession))
        assert bot_session is not None
        assert bot_session.status_message_id == 2

    texts = [text for _, text, _ in fake_api.messages]
    assert any("Штрихкод принят" in text for text in texts)

    engine.dispose()
    if os.path.exists(path):
        os.remove(path)


def test_logistics_telegram_bot_callback_menu_flow() -> None:
    engine, path = setup_db()
    ids = seed_data(engine)
    fake_api = FakeBotApi()
    bot = LogisticsTelegramBot(fake_api, engine)

    bot.handle_update(_message(101, "sender_user", "/start"))
    start_markup = fake_api.messages[-1][2]
    assert start_markup is not None
    assert start_markup["inline_keyboard"][0][0]["text"] == "📦 Передать"

    bot.handle_update(
        {
            "callback_query": {
                "id": "cb-1",
                "data": "menu:handoff",
                "from": {"id": 101, "username": "sender_user"},
                "message": {"chat": {"id": 101}},
            }
        }
    )
    assert fake_api.callback_answers[-1] == ("cb-1", None)
    driver_markup = fake_api.messages[-1][2]
    assert driver_markup["inline_keyboard"][0][0]["callback_data"] == "handoff_driver:1"

    bot.handle_update(
        {
            "callback_query": {
                "id": "cb-2",
                "data": "handoff_driver:1",
                "from": {"id": 101, "username": "sender_user"},
                "message": {"chat": {"id": 101}},
            }
        }
    )
    dropoff_markup = fake_api.messages[-1][2]
    assert any(
        button["callback_data"] == "handoff_dropoff:1:2"
        for row in dropoff_markup["inline_keyboard"]
        for button in row
    )

    bot.handle_update(
        {
            "callback_query": {
                "id": "cb-3",
                "data": "handoff_dropoff:1:2",
                "from": {"id": 101, "username": "sender_user"},
                "message": {"chat": {"id": 101}},
            }
        }
    )
    bot.handle_update(_message(101, "sender_user", "BC-0001"))
    bot.handle_update(
        {
            "callback_query": {
                "id": "cb-4",
                "data": "draft:status",
                "from": {"id": 101, "username": "sender_user"},
                "message": {"chat": {"id": 101}},
            }
        }
    )

    bot.handle_update(
        {
            "callback_query": {
                "id": "cb-5",
                "data": "draft:confirm",
                "from": {"id": 101, "username": "sender_user"},
                "message": {"chat": {"id": 101}},
            }
        }
    )

    texts = _all_texts(fake_api)
    assert any("Создан черновик передачи" in text for text in texts)
    assert any("Последние сканы:" in text for text in texts)
    assert any("подтвержден" in text for text in texts)
    assert fake_api.edits

    with Session(engine) as session:
        history = logistics_service.get_transfer_history(session, transfer_id=ids["transfer_id"])
        assert history[0]["event_type"] == "handed_to_driver"
        assert history[0]["source"] == "telegram"

    engine.dispose()
    if os.path.exists(path):
        os.remove(path)


def test_logistics_telegram_bot_aggregates_scan_errors_and_ui_events() -> None:
    engine, path = setup_db()
    ids = seed_data(engine)
    fake_api = FakeBotApi()
    bot = LogisticsTelegramBot(fake_api, engine)

    bot.handle_update(_message(101, "sender_user", "/handoff 1 2"))
    bot.handle_update(_message(101, "sender_user", "UNKNOWN-BARCODE"))
    bot.handle_update(_message(101, "sender_user", "BC-0001"))
    bot.handle_update(
        {
            "callback_query": {
                "id": "cb-err-1",
                "data": "draft:incident_picker",
                "from": {"id": 101, "username": "sender_user"},
                "message": {"chat": {"id": 101}},
            }
        }
    )
    bot.handle_update(
        {
            "callback_query": {
                "id": "cb-err-2",
                "data": "draft:incident:1",
                "from": {"id": 101, "username": "sender_user"},
                "message": {"chat": {"id": 101}},
            }
        }
    )
    bot.handle_update(
        {
            "callback_query": {
                "id": "cb-err-3",
                "data": "draft:return_picker",
                "from": {"id": 101, "username": "sender_user"},
                "message": {"chat": {"id": 101}},
            }
        }
    )
    bot.handle_update(
        {
            "callback_query": {
                "id": "cb-err-4",
                "data": "draft:return:1",
                "from": {"id": 101, "username": "sender_user"},
                "message": {"chat": {"id": 101}},
            }
        }
    )

    texts = _all_texts(fake_api)
    assert any("Ошибка сканирования:" in text for text in texts)
    assert any("Ошибки сканирования: 1" in text for text in texts)
    assert any("Инцидент зафиксирован." in text for text in texts)
    assert any("Возврат зафиксирован." in text for text in texts)
    assert fake_api.edits

    with Session(engine) as session:
        history = logistics_service.get_transfer_history(session, transfer_id=ids["transfer_id"])
        event_types = [event["event_type"] for event in history]
        assert "incident" in event_types
        assert "returned" in event_types
        assert all(event["source"] == "telegram" for event in history)
        bot_session = session.query(LogisticsBotSession).one()
        assert bot_session.scan_error_count == 1
        assert bot_session.recent_errors

    engine.dispose()
    if os.path.exists(path):
        os.remove(path)


def test_logistics_telegram_bot_reuses_existing_open_draft_instead_of_creating_new_one() -> None:
    engine, path = setup_db()
    seed_data(engine)
    fake_api = FakeBotApi()
    bot = LogisticsTelegramBot(fake_api, engine)

    bot.handle_update(_message(101, "sender_user", "/handoff 1 2"))
    bot.handle_update(_message(101, "sender_user", "/handoff 1 2"))

    texts = _all_texts(fake_api)
    assert any("У вас уже есть открытый черновик." in text for text in texts)
    assert any("Продолжаем черновик передачи #1." in text for text in texts)

    with Session(engine) as session:
        drafts = session.scalars(select(LogisticsDraft)).all()
        assert len(drafts) == 1
        bot_session = session.query(LogisticsBotSession).one()
        assert bot_session.draft_id == drafts[0].id

    engine.dispose()
    if os.path.exists(path):
        os.remove(path)


def test_logistics_telegram_bot_stops_on_legacy_multiple_open_drafts() -> None:
    engine, path = setup_db()
    ids = seed_data(engine)

    with Session(engine) as session:
        session.execute(text("DROP INDEX IF EXISTS ix_logistics_draft_actor_open_unique"))
        session.add_all(
            [
                LogisticsDraft(
                    draft_type="handoff",
                    status="open",
                    warehouse_id=1,
                    actor_user_id=ids["sender_id"],
                    driver_id=1,
                    default_dropoff_warehouse_id=2,
                ),
                LogisticsDraft(
                    draft_type="receipt",
                    status="open",
                    warehouse_id=1,
                    actor_user_id=ids["sender_id"],
                ),
            ]
        )
        session.commit()

    fake_api = FakeBotApi()
    bot = LogisticsTelegramBot(fake_api, engine)

    bot.handle_update(_message(101, "sender_user", "BC-0001"))
    bot.handle_update(
        {
            "message": {
                "chat": {"id": 101},
                "from": {"id": 101, "username": "sender_user"},
                "photo": [{"file_id": "photo-small"}, {"file_id": "photo-big"}],
            }
        }
    )

    texts = _all_texts(fake_api)
    assert any("Обнаружено несколько открытых черновиков" in text for text in texts)

    with Session(engine) as session:
        assert session.query(LogisticsBotSession).count() == 0
        assert session.query(LogisticsBotSessionPhoto).count() == 0

    engine.dispose()
    if os.path.exists(path):
        os.remove(path)
