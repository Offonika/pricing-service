#!/usr/bin/env python3
"""Plan or register the Bitrix pickup bot, command and dedicated SMS marker field."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_settings  # noqa: E402
from app.services.site_order_fulfillment import BitrixChatClient  # noqa: E402

BOT_CODE = "mm_pickup_fulfillment"
SMS_FIELD_TITLE = "SMS «Самовывоз готов к выдаче» отправлена"


def build_plan() -> dict[str, object]:
    settings = get_settings()
    return {
        "bot": {
            "CODE": BOT_CODE,
            "TYPE": "S",
            "EVENT_HANDLER": settings.order_fulfillment_bot_callback_url,
            "CLIENT_ID": settings.order_fulfillment_bot_client_id,
            "PROPERTIES": {
                "NAME": "Самовывоз Master Mobile",
                "COLOR": "AQUA",
                "EMAIL": "",
                "PERSONAL_BIRTHDAY": "2026-08-23",
                "WORK_POSITION": "Подтверждение событий самовывоза",
            },
        },
        "command": {
            "COMMAND": settings.order_fulfillment_bot_command,
            "CLIENT_ID": settings.order_fulfillment_bot_client_id,
            "COMMON": "N",
            "HIDDEN": "Y",
            "EXTRANET_SUPPORT": "N",
            "EVENT_COMMAND_ADD": settings.order_fulfillment_bot_callback_url,
            "LANG": [{"LANGUAGE_ID": "ru", "TITLE": "Действие самовывоза", "PARAMS": ""}],
        },
        "existing_command_id": settings.order_fulfillment_bot_command_id,
        "deal_user_field": {
            "FIELD_NAME": settings.order_fulfillment_bot_pickup_sms_field,
            "USER_TYPE_ID": "datetime",
            "EDIT_FORM_LABEL": {"ru": SMS_FIELD_TITLE},
            "LIST_COLUMN_LABEL": {"ru": SMS_FIELD_TITLE},
            "LIST_FILTER_LABEL": {"ru": SMS_FIELD_TITLE},
            "XML_ID": "MM_PICKUP_READY_SMS_AT",
            "MULTIPLE": "N",
            "MANDATORY": "N",
            "SHOW_FILTER": "I",
            "SHOW_IN_LIST": "Y",
        },
    }


def apply_plan(
    client: BitrixChatClient,
    plan: dict[str, object],
    *,
    recover_missing_command: bool = False,
) -> dict[str, object]:
    field = dict(plan["deal_user_field"])
    existing_fields = client.list_deal_user_fields(str(field["FIELD_NAME"]))
    if len(existing_fields) > 1:
        raise RuntimeError("multiple deal user fields found for the configured field name")
    if existing_fields:
        actual_user_type = str(existing_fields[0].get("USER_TYPE_ID") or "").casefold()
        expected_user_type = str(field.get("USER_TYPE_ID") or "").casefold()
        if actual_user_type != expected_user_type:
            raise RuntimeError(
                f"existing deal user field type is {actual_user_type or 'unknown'}, "
                f"expected {expected_user_type}"
            )
        existing_field_id = _item_id(existing_fields[0])
        if existing_field_id is None:
            raise RuntimeError("existing deal user field returned without a valid positive id")
    else:
        existing_field_id = None

    existing_bot = next(
        (
            item
            for item in client.list_bots()
            if str(item.get("CODE") or item.get("code") or "").casefold() == BOT_CODE.casefold()
        ),
        None,
    )
    bot_id = _item_id(existing_bot) if existing_bot is not None else None
    command_id = _item_id({"ID": plan.get("existing_command_id")})
    if existing_bot is not None:
        if bot_id is None:
            raise RuntimeError("existing bot returned without a valid positive id")
        actual_type = str(existing_bot.get("TYPE") or existing_bot.get("type") or "").upper()
        expected_type = str(dict(plan["bot"]).get("TYPE") or "").upper()
        if not actual_type or actual_type != expected_type:
            raise RuntimeError(
                f"existing bot type is {actual_type or 'unknown'}, expected {expected_type}; "
                "imbot.update cannot change TYPE, so a separately confirmed "
                "unregister/register is required"
            )
    if bot_id is not None and command_id is None and not recover_missing_command:
        raise RuntimeError(
            "existing bot found; ORDER_FULFILLMENT_BOT_COMMAND_ID is required "
            "because imbot.command.list is unavailable on this portal; after verifying "
            "in Bitrix that the command is absent, rerun with --recover-missing-command"
        )

    field_result = existing_field_id or _result_id(client.add_deal_user_field(field))
    if field_result is None:
        raise RuntimeError("crm.deal.userfield.add returned an empty field id")
    if bot_id is None:
        bot_id = _result_id(client.register_bot(dict(plan["bot"])))
        if bot_id is None:
            raise RuntimeError("imbot.register returned an empty bot id")
        is_new_bot = True
    else:
        if not client.update_bot(bot_id, dict(plan["bot"])):
            raise RuntimeError("imbot.update returned an empty result")
        is_new_bot = False
    command = dict(plan["command"])
    if is_new_bot or command_id is None:
        command_id = _result_id(client.register_bot_command({**command, "BOT_ID": bot_id}))
        if command_id is None:
            raise RuntimeError("imbot.command.register returned an empty command id")
    else:
        assert command_id is not None
        if not client.update_bot_command(command_id, command):
            raise RuntimeError("imbot.command.update returned an empty result")
    return {"bot_id": bot_id, "command_id": command_id, "deal_user_field_id": field_result}


def _item_id(item: dict[str, object] | None) -> int | str | None:
    if item is None:
        return None
    value = item.get("ID") or item.get("BOT_ID") or item.get("COMMAND_ID") or item.get("id")
    normalized = str(value or "").strip()
    if not normalized.isdigit() or int(normalized) <= 0:
        return None
    return int(normalized)


def _result_id(value: object) -> int | str | None:
    if isinstance(value, dict):
        return _item_id(value)
    return _item_id({"ID": value})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Perform external Bitrix changes")
    parser.add_argument(
        "--recover-missing-command",
        action="store_true",
        help=(
            "Register a command for an existing bot only after its absence was verified " "manually"
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = get_settings()
    plan = build_plan()
    output: dict[str, object] = {"mode": "apply" if args.apply else "dry-run", "plan": plan}
    if args.apply:
        if not str(settings.order_fulfillment_bitrix_webhook_url or "").strip():
            raise SystemExit("ORDER_FULFILLMENT_BITRIX_WEBHOOK_URL is not configured")
        callback_url = str(settings.order_fulfillment_bot_callback_url or "").strip()
        if not callback_url:
            raise SystemExit("ORDER_FULFILLMENT_BOT_CALLBACK_URL is not configured")
        if not callback_url.casefold().startswith("https://"):
            raise SystemExit("ORDER_FULFILLMENT_BOT_CALLBACK_URL must use HTTPS")
        if not str(settings.order_fulfillment_bot_client_id or "").strip():
            raise SystemExit("ORDER_FULFILLMENT_BOT_CLIENT_ID is not configured")
        output["result"] = apply_plan(
            BitrixChatClient(
                settings.order_fulfillment_bitrix_webhook_url,
                bot_client_id=settings.order_fulfillment_bot_client_id,
            ),
            plan,
            recover_missing_command=args.recover_missing_command,
        )
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
