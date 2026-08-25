from __future__ import annotations

import pytest

from scripts import publish_order_fulfillment_bot_menu as publish


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def add_bot_message(self, **kwargs):
        self.calls.append(("add", kwargs))
        return "701"

    def update_bot_message(self, **kwargs):
        self.calls.append(("update", kwargs))
        return True


def _plan(*, message_id: int | None = None) -> dict[str, object]:
    return {
        "dialog_id": "chat8729",
        "bot_id": 42,
        "menu_message_id": message_id,
        "message": "Самовывоз Master Mobile",
        "keyboard": [{"TEXT": "Найти заказ", "ACTION": "PUT"}],
    }


def test_missing_menu_fails_closed_without_recovery() -> None:
    client = FakeClient()

    with pytest.raises(RuntimeError, match="recover-missing-menu"):
        publish.apply_plan(client, _plan())  # type: ignore[arg-type]

    assert client.calls == []


def test_first_menu_is_created_only_with_explicit_recovery() -> None:
    client = FakeClient()

    result = publish.apply_plan(  # type: ignore[arg-type]
        client,
        _plan(),
        recover_missing_menu=True,
    )

    assert result == {"menu_message_id": 701, "created": True}
    assert client.calls[0][0] == "add"
    assert client.calls[0][1]["dialog_id"] == "chat8729"


def test_existing_menu_is_updated_in_place() -> None:
    client = FakeClient()

    result = publish.apply_plan(client, _plan(message_id=701))  # type: ignore[arg-type]

    assert result == {"menu_message_id": 701, "created": False}
    assert client.calls[0][0] == "update"
    assert client.calls[0][1]["message_id"] == "701"
