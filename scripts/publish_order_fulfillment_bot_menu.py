#!/usr/bin/env python3
"""Plan, publish or refresh the reusable Russian pickup-bot menu card."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_settings  # noqa: E402
from app.services import site_order_fulfillment_bot as bot  # noqa: E402
from app.services.site_order_fulfillment import BitrixChatClient  # noqa: E402


def build_plan() -> dict[str, Any]:
    settings = get_settings()
    return {
        "dialog_id": settings.order_fulfillment_pickup_ready_chat_dialog_id,
        "bot_id": settings.order_fulfillment_bot_id,
        "menu_message_id": settings.order_fulfillment_bot_menu_message_id,
        "message": bot.pickup_menu_text(),
        "keyboard": bot.pickup_menu_keyboard(),
    }


def apply_plan(
    client: BitrixChatClient,
    plan: dict[str, Any],
    *,
    recover_missing_menu: bool = False,
) -> dict[str, Any]:
    dialog_id = str(plan.get("dialog_id") or "").strip()
    bot_id = int(plan.get("bot_id") or 0)
    menu_message_id = int(plan.get("menu_message_id") or 0)
    if not dialog_id or bot_id <= 0:
        raise RuntimeError("pickup menu dialog or bot id is not configured")
    payload = {
        "bot_id": bot_id,
        "message": str(plan.get("message") or ""),
        "keyboard": list(plan.get("keyboard") or []),
    }
    if menu_message_id > 0:
        client.update_bot_message(message_id=str(menu_message_id), **payload)
        return {"menu_message_id": menu_message_id, "created": False}
    if not recover_missing_menu:
        raise RuntimeError(
            "ORDER_FULFILLMENT_BOT_MENU_MESSAGE_ID is not configured; after verifying "
            "that no reusable menu exists, rerun with --recover-missing-menu"
        )
    created_id = client.add_bot_message(dialog_id=dialog_id, **payload)
    return {"menu_message_id": int(created_id), "created": True}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Publish or update in Bitrix")
    parser.add_argument(
        "--recover-missing-menu",
        action="store_true",
        help="Create the first menu only after confirming that no reusable menu exists",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = get_settings()
    plan = build_plan()
    output: dict[str, Any] = {"mode": "apply" if args.apply else "dry-run", "plan": plan}
    if args.apply:
        if not settings.order_fulfillment_bot_enabled:
            raise SystemExit("ORDER_FULFILLMENT_BOT_ENABLED is false")
        if not str(settings.order_fulfillment_bitrix_webhook_url or "").strip():
            raise SystemExit("ORDER_FULFILLMENT_BITRIX_WEBHOOK_URL is not configured")
        if plan["dialog_id"] not in set(settings.order_fulfillment_bot_source_chat_ids):
            raise SystemExit("pickup menu dialog is not in the bot source-chat allowlist")
        output["result"] = apply_plan(
            BitrixChatClient(
                settings.order_fulfillment_bitrix_webhook_url,
                bot_client_id=settings.order_fulfillment_bot_client_id,
            ),
            plan,
            recover_missing_menu=args.recover_missing_menu,
        )
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
