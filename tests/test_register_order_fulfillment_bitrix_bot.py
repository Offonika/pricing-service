from __future__ import annotations

import pytest

from app.services import site_order_fulfillment as fulfillment
from scripts import register_order_fulfillment_bitrix_bot as register


class FakeRegistrationClient:
    def __init__(
        self,
        *,
        existing_bot: bool = False,
        existing_field: bool = True,
    ) -> None:
        self.existing_bot = existing_bot
        self.existing_field = existing_field
        self.calls: list[tuple[str, object]] = []

    def list_bots(self):
        self.calls.append(("list_bots", None))
        return [{"ID": "42", "CODE": register.BOT_CODE, "TYPE": "S"}] if self.existing_bot else []

    def register_bot(self, fields):
        self.calls.append(("register_bot", fields))
        self.existing_bot = True
        return 42

    def update_bot(self, bot_id, fields):
        self.calls.append(("update_bot", {"bot_id": bot_id, "fields": fields}))
        return True

    def register_bot_command(self, fields):
        self.calls.append(("register_bot_command", fields))
        return 103

    def update_bot_command(self, command_id, fields):
        self.calls.append(("update_bot_command", {"command_id": command_id, "fields": fields}))
        return True

    def list_deal_user_fields(self, field_name):
        self.calls.append(("list_deal_user_fields", field_name))
        return [{"ID": "77", "USER_TYPE_ID": "datetime"}] if self.existing_field else []

    def add_deal_user_field(self, fields):
        self.calls.append(("add_deal_user_field", fields))
        self.existing_field = True
        return 77


def _plan(
    *,
    bot_id: int | None = None,
    command_id: int | None = None,
) -> dict[str, object]:
    return {
        "bot": {
            "CODE": register.BOT_CODE,
            "TYPE": "S",
            "CLIENT_ID": "pickup-bot",
        },
        "command": {
            "COMMAND": "pickup_action",
            "CLIENT_ID": "pickup-bot",
        },
        "existing_bot_id": bot_id,
        "existing_command_id": command_id,
        "deal_user_field": {
            "FIELD_NAME": "UF_CRM_MM_PICKUP_READY_SMS_AT",
            "USER_TYPE_ID": "datetime",
        },
    }


def test_first_registration_creates_bot_and_command() -> None:
    client = FakeRegistrationClient(existing_field=False)

    result = register.apply_plan(client, _plan())  # type: ignore[arg-type]

    assert result == {"bot_id": 42, "command_id": 103, "deal_user_field_id": 77}
    command_call = next(value for name, value in client.calls if name == "register_bot_command")
    assert command_call["BOT_ID"] == 42
    call_names = [name for name, _ in client.calls]
    assert call_names.index("add_deal_user_field") < call_names.index("register_bot")
    assert call_names.index("register_bot") < call_names.index("register_bot_command")
    assert all(name != "update_bot_command" for name, _ in client.calls)


def test_existing_registration_uses_explicit_command_id_and_update_methods() -> None:
    client = FakeRegistrationClient(existing_bot=True)

    result = register.apply_plan(client, _plan(command_id=103))  # type: ignore[arg-type]

    assert result["bot_id"] == 42
    assert result["command_id"] == 103
    command_call = next(value for name, value in client.calls if name == "update_bot_command")
    assert command_call["command_id"] == 103
    assert "BOT_ID" not in command_call["fields"]
    assert all(name != "register_bot_command" for name, _ in client.calls)


def test_existing_registration_without_command_id_fails_before_any_update() -> None:
    client = FakeRegistrationClient(existing_bot=True)

    with pytest.raises(RuntimeError, match="ORDER_FULFILLMENT_BOT_COMMAND_ID"):
        register.apply_plan(client, _plan())  # type: ignore[arg-type]

    assert client.calls == [
        ("list_deal_user_fields", "UF_CRM_MM_PICKUP_READY_SMS_AT"),
        ("list_bots", None),
    ]


def test_partial_bot_registration_can_recover_verified_missing_command() -> None:
    client = FakeRegistrationClient()
    original_register_command = client.register_bot_command
    first_call = True

    def ambiguous_register_command(fields):
        nonlocal first_call
        result = original_register_command(fields)
        if first_call:
            first_call = False
            return None
        return result

    client.register_bot_command = ambiguous_register_command  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="empty command id"):
        register.apply_plan(client, _plan())  # type: ignore[arg-type]

    assert client.existing_bot is True
    client.calls.clear()
    result = register.apply_plan(  # type: ignore[arg-type]
        client,
        _plan(),
        recover_missing_command=True,
    )

    assert result["bot_id"] == 42
    assert result["command_id"] == 103
    command_call = next(value for name, value in client.calls if name == "register_bot_command")
    assert command_call["BOT_ID"] == 42
    assert all(name != "update_bot_command" for name, _ in client.calls)


def test_existing_bot_with_wrong_type_fails_before_any_update() -> None:
    client = FakeRegistrationClient(existing_bot=True)
    original_list = client.list_bots

    def wrong_type_list():
        original_list()
        return [{"ID": "42", "CODE": register.BOT_CODE, "TYPE": "B"}]

    client.list_bots = wrong_type_list  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="cannot change TYPE"):
        register.apply_plan(client, _plan(command_id=103))  # type: ignore[arg-type]

    assert client.calls == [
        ("list_deal_user_fields", "UF_CRM_MM_PICKUP_READY_SMS_AT"),
        ("list_bots", None),
    ]


def test_existing_bot_without_type_uses_pinned_bot_id() -> None:
    client = FakeRegistrationClient(existing_bot=True)

    def bot_without_type():
        client.calls.append(("list_bots", None))
        return [{"ID": "42", "CODE": register.BOT_CODE}]

    client.list_bots = bot_without_type  # type: ignore[method-assign]

    result = register.apply_plan(  # type: ignore[arg-type]
        client,
        _plan(bot_id=42, command_id=103),
    )

    assert result["bot_id"] == 42
    assert any(name == "update_bot" for name, _ in client.calls)


def test_existing_bot_without_type_and_without_pinned_id_fails_closed() -> None:
    client = FakeRegistrationClient(existing_bot=True)

    def bot_without_type():
        client.calls.append(("list_bots", None))
        return [{"ID": "42", "CODE": register.BOT_CODE}]

    client.list_bots = bot_without_type  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="does not expose TYPE"):
        register.apply_plan(client, _plan(command_id=103))  # type: ignore[arg-type]

    assert client.calls == [
        ("list_deal_user_fields", "UF_CRM_MM_PICKUP_READY_SMS_AT"),
        ("list_bots", None),
    ]


def test_configured_bot_id_without_matching_bot_fails_closed() -> None:
    client = FakeRegistrationClient()

    with pytest.raises(RuntimeError, match="configured but the bot was not found"):
        register.apply_plan(  # type: ignore[arg-type]
            client,
            _plan(bot_id=42, command_id=103),
        )

    assert all(name not in {"register_bot", "register_bot_command"} for name, _ in client.calls)


def test_existing_bot_without_valid_id_fails_before_any_update() -> None:
    client = FakeRegistrationClient(existing_bot=True)

    def bot_without_id():
        client.calls.append(("list_bots", None))
        return [{"CODE": register.BOT_CODE, "TYPE": "S"}]

    client.list_bots = bot_without_id  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="without a valid positive id"):
        register.apply_plan(client, _plan(command_id=103))  # type: ignore[arg-type]

    assert client.calls == [
        ("list_deal_user_fields", "UF_CRM_MM_PICKUP_READY_SMS_AT"),
        ("list_bots", None),
    ]


def test_existing_field_without_valid_id_fails_before_bot_update() -> None:
    client = FakeRegistrationClient(existing_bot=True)

    def field_without_id(_field_name):
        client.calls.append(("list_deal_user_fields", _field_name))
        return [{"USER_TYPE_ID": "datetime"}]

    client.list_deal_user_fields = field_without_id  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="without a valid positive id"):
        register.apply_plan(client, _plan(command_id=103))  # type: ignore[arg-type]

    assert client.calls == [
        ("list_deal_user_fields", "UF_CRM_MM_PICKUP_READY_SMS_AT"),
    ]


def test_ambiguous_field_creation_is_recoverable_before_bot_registration() -> None:
    client = FakeRegistrationClient(existing_field=False)
    original_add = client.add_deal_user_field
    first_call = True

    def ambiguous_add(fields):
        nonlocal first_call
        result = original_add(fields)
        if first_call:
            first_call = False
            return None
        return result

    client.add_deal_user_field = ambiguous_add  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="empty field id"):
        register.apply_plan(client, _plan())  # type: ignore[arg-type]

    assert all(name not in {"register_bot", "register_bot_command"} for name, _ in client.calls)
    client.calls.clear()

    result = register.apply_plan(client, _plan())  # type: ignore[arg-type]

    assert result == {
        "bot_id": 42,
        "command_id": 103,
        "deal_user_field_id": 77,
    }
    assert all(name != "add_deal_user_field" for name, _ in client.calls)


def test_bitrix_update_methods_use_nested_fields_and_outer_client_id() -> None:
    client = fulfillment.BitrixChatClient(
        "https://crm.example/rest/1/token",
        bot_client_id="pickup-bot",
    )
    calls: list[tuple[str, dict]] = []

    def fake_call(method: str, params: dict | None = None) -> dict:
        calls.append((method, params or {}))
        return {"result": True}

    client.call = fake_call  # type: ignore[method-assign]
    client.update_bot(
        42,
        {"CODE": register.BOT_CODE, "TYPE": "S", "CLIENT_ID": "pickup-bot"},
    )
    client.update_bot_command(
        103,
        {"COMMAND": "pickup_action", "CLIENT_ID": "pickup-bot"},
    )

    assert calls == [
        (
            "imbot.update",
            {
                "BOT_ID": 42,
                "FIELDS": {"CODE": register.BOT_CODE},
                "CLIENT_ID": "pickup-bot",
            },
        ),
        (
            "imbot.command.update",
            {
                "COMMAND_ID": 103,
                "FIELDS": {"COMMAND": "pickup_action"},
                "CLIENT_ID": "pickup-bot",
            },
        ),
    ]
