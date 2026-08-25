#!/usr/bin/env python3
"""Backfill Bitrix order chats without CRM/SMS/task side effects."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, inspect, select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_settings  # noqa: E402
from app.infrastructure.db import session_scope  # noqa: E402
from app.models.logistics import LogisticsWarehouse  # noqa: E402
from app.models.site_order_fulfillment import (  # noqa: E402
    BitrixChatMention,
    BitrixChatMessage,
    BitrixChatReaction,
    PickupInventoryItem,
    PickupInventoryRun,
    PickupInventorySubmission,
    SiteOrderExecutionCase,
    SiteOrderExecutionEvent,
)
from app.services import pickup_control  # noqa: E402
from app.services import site_order_fulfillment as fulfillment  # noqa: E402
from infra.cron import order_fulfillment_sync as fulfillment_sync  # noqa: E402

SHARED_ENV_FILE = Path("/etc/mm-management-orchestrator.env")
APPLICATION_ENV_FILE = PROJECT_ROOT / ".env"
RAW_BACKFILL_SCHEMA: dict[str, set[str]] = {
    LogisticsWarehouse.__tablename__: {"id", "external_id", "kind", "is_active"},
    BitrixChatMessage.__tablename__: {
        "id",
        "chat_code",
        "chat_id",
        "message_id",
        "payload",
    },
    BitrixChatMention.__tablename__: {"id", "message_id", "site_order_number"},
    BitrixChatReaction.__tablename__: {
        "id",
        "message_id",
        "actor_id",
        "is_active",
    },
    SiteOrderExecutionCase.__tablename__: {
        "id",
        "site_order_number",
        "notification_confirmed_at",
        "sla_started_at",
        "hold_until",
    },
    SiteOrderExecutionEvent.__tablename__: {
        "id",
        "case_id",
        "warehouse_id",
        "actor_ref",
    },
    PickupInventoryRun.__tablename__: {"id", "dialog_id", "business_date"},
    PickupInventorySubmission.__tablename__: {
        "id",
        "run_id",
        "warehouse_id",
        "source_message_id",
        "revision",
    },
    PickupInventoryItem.__tablename__: {"id", "submission_id", "site_order_number"},
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--chat",
        action="append",
        choices=("all", "site", "pickup-ready", "inventory", "movement", "exception", "courier"),
        default=[],
    )
    parser.add_argument("--page-size", type=int, default=50)
    parser.add_argument("--max-pages", type=int, default=200)
    parser.add_argument("--lookback-since", type=datetime.fromisoformat, default=None)
    parser.add_argument(
        "--persist-raw",
        action="store_true",
        help="Persist redacted messages/reactions/inventory locally. Default only reports counts.",
    )
    return parser.parse_args()


def chat_sources(settings: Any, selected: list[str]) -> list[tuple[str, str]]:
    mapping = {
        "site": (
            fulfillment.CHAT_SITE_MASTER_MOBILE,
            settings.order_fulfillment_site_chat_dialog_id,
        ),
        "pickup-ready": (
            fulfillment.CHAT_PICKUP_READY,
            settings.order_fulfillment_pickup_ready_chat_dialog_id,
        ),
        "inventory": (
            fulfillment.CHAT_PICKUP_INVENTORY,
            settings.order_fulfillment_pickup_inventory_chat_dialog_id,
        ),
        "movement": (
            fulfillment.CHAT_PICKUP_MOVEMENT,
            settings.order_fulfillment_pickup_movement_chat_dialog_id,
        ),
        "exception": (
            fulfillment.CHAT_PICKUP_EXCEPTION,
            settings.order_fulfillment_pickup_exception_chat_dialog_id,
        ),
        "courier": (
            fulfillment.CHAT_COURIER_SPB,
            settings.order_fulfillment_spb_courier_chat_dialog_id,
        ),
    }
    names = list(mapping) if not selected or "all" in selected else list(dict.fromkeys(selected))
    return [mapping[name] for name in names]


def configure_runtime_environment(*, require_database: bool) -> None:
    """Load release configuration without borrowing another service's database."""

    shared_values = fulfillment_sync.load_env_files((SHARED_ENV_FILE,))
    application_values = fulfillment_sync.load_env_files((APPLICATION_ENV_FILE,))
    shared_values.pop("DATABASE_URL", None)
    fulfillment_sync.apply_env_defaults({**shared_values, **application_values})
    if require_database and not os.environ.get("DATABASE_URL"):
        raise SystemExit(
            "Application DATABASE_URL is not configured in the process environment "
            f"or {APPLICATION_ENV_FILE}"
        )
    get_settings.cache_clear()


def ensure_raw_backfill_schema(session) -> None:
    """Refuse persistence when the selected DB is not the migrated application DB."""

    schema_inspector = inspect(session.get_bind())
    available_tables = set(schema_inspector.get_table_names())
    missing_tables = sorted(set(RAW_BACKFILL_SCHEMA) - available_tables)
    missing_columns: list[str] = []
    for table_name, required_columns in RAW_BACKFILL_SCHEMA.items():
        if table_name not in available_tables:
            continue
        available_columns = {
            str(column["name"]) for column in schema_inspector.get_columns(table_name)
        }
        missing_columns.extend(
            f"{table_name}.{column_name}"
            for column_name in sorted(required_columns - available_columns)
        )
    if missing_tables or missing_columns:
        details: list[str] = []
        if missing_tables:
            details.append(f"missing tables: {', '.join(missing_tables)}")
        if missing_columns:
            details.append(f"missing columns: {', '.join(missing_columns)}")
        raise RuntimeError(
            "Raw backfill database preflight failed; "
            + "; ".join(details)
            + ". Run the command from the active pricing-service release or set "
            "its DATABASE_URL explicitly."
        )


def inspect_pages(
    client: fulfillment.BitrixChatClient,
    *,
    dialog_id: str,
    page_size: int,
    max_pages: int,
    lookback_since: datetime | None,
) -> dict[str, Any]:
    lookback_since = fulfillment._naive_utc_datetime(lookback_since)  # noqa: SLF001
    last_id: int | None = None
    seen: set[int] = set()
    stats: dict[str, Any] = {
        "pages": 0,
        "messages": 0,
        "reaction_messages": 0,
        "oldest_message_id": None,
        "newest_message_id": None,
        "page_limit_reached": False,
    }
    for _ in range(max(1, max_pages)):
        result = client.get_dialog_messages(
            dialog_id,
            limit=max(1, min(page_size, 50)),
            last_id=last_id,
        )
        messages = fulfillment._list_items(result.get("messages") or [])  # noqa: SLF001
        ids = [
            value
            for item in messages
            if (value := fulfillment._int_or_none(item.get("id") or item.get("ID")))  # noqa: SLF001
            is not None
        ]
        if not ids:
            break
        oldest = min(ids)
        if oldest in seen:
            break
        seen.add(oldest)
        stats["pages"] += 1
        stats["messages"] += len(messages)
        stats["reaction_messages"] += sum(
            1
            for item in messages
            if (
                (item.get("params") or {}).get("LIKE")
                if isinstance(item.get("params"), dict)
                else None
            )
        )
        stats["oldest_message_id"] = oldest
        stats["newest_message_id"] = max(max(ids), int(stats["newest_message_id"] or 0))
        dates = [
            normalized
            for item in messages
            if (parsed := fulfillment.parse_datetime(item.get("date"))) is not None
            and (normalized := fulfillment._naive_utc_datetime(parsed)) is not None  # noqa: SLF001
        ]
        if lookback_since is not None and dates and min(dates) <= lookback_since:
            break
        last_id = oldest
    else:
        stats["page_limit_reached"] = True
    return stats


def build_verification_report(session) -> dict[str, Any]:
    messages = session.scalars(select(BitrixChatMessage)).all()
    orders_by_chat: dict[str, set[str]] = {}
    for message in messages:
        orders_by_chat.setdefault(message.chat_code, set()).update(
            fulfillment.bitrix_message_order_numbers(message)
        )
    site_orders = orders_by_chat.get(fulfillment.CHAT_SITE_MASTER_MOBILE, set())
    pickup_orders = orders_by_chat.get(fulfillment.CHAT_PICKUP_READY, set())
    return {
        "message_count": len(messages),
        "active_pickup_warehouse_count": int(
            session.scalar(
                select(func.count(LogisticsWarehouse.id)).where(
                    LogisticsWarehouse.is_active.is_(True),
                    LogisticsWarehouse.kind.in_(["store", "retail"]),
                )
            )
            or 0
        ),
        "inventory_confirmed": int(
            session.scalar(
                select(func.count(PickupInventorySubmission.id)).where(
                    PickupInventorySubmission.status == "confirmed"
                )
            )
            or 0
        ),
        "inventory_manual_review": int(
            session.scalar(
                select(func.count(PickupInventorySubmission.id)).where(
                    PickupInventorySubmission.status == "manual_review"
                )
            )
            or 0
        ),
        "inventory_confirmed_warehouse_count": int(
            session.scalar(
                select(func.count(func.distinct(PickupInventorySubmission.warehouse_id))).where(
                    PickupInventorySubmission.status == "confirmed",
                    PickupInventorySubmission.warehouse_id.is_not(None),
                )
            )
            or 0
        ),
        "site_to_pickup_chain_order_count": len(site_orders & pickup_orders),
    }


def main() -> int:
    args = parse_args()
    configure_runtime_environment(require_database=args.persist_raw)
    settings = get_settings()
    webhook_url = fulfillment_sync.resolve_bitrix_webhook_url()
    if not webhook_url:
        raise SystemExit("Bitrix webhook is not configured")
    client = fulfillment.BitrixChatClient(
        webhook_url,
        bot_client_id=settings.order_fulfillment_bot_client_id,
    )
    output: dict[str, Any] = {
        "mode": "persist_raw" if args.persist_raw else "dry_run",
        "chats": {},
    }
    sources = chat_sources(settings, args.chat)
    if not args.persist_raw:
        for chat_code, dialog_id in sources:
            output["chats"][chat_code] = inspect_pages(
                client,
                dialog_id=dialog_id,
                page_size=args.page_size,
                max_pages=args.max_pages,
                lookback_since=args.lookback_since,
            )
    else:
        with session_scope() as session:
            ensure_raw_backfill_schema(session)
            for chat_code, dialog_id in sources:
                output["chats"][chat_code] = fulfillment.poll_bitrix_chat_pages(
                    session,
                    client=client,
                    chat_code=chat_code,
                    dialog_id=dialog_id,
                    page_size=args.page_size,
                    max_pages=args.max_pages,
                    lookback_since=args.lookback_since,
                    run_ocr=False,
                    settings=settings,
                )
            output["inventory"] = pickup_control.persist_pending_inventory_messages(
                session,
                limit=5000,
                order_exists=pickup_control.build_crm_order_exists_probe(client),
            )
            output["verification"] = build_verification_report(session)
            session.commit()
    print(json.dumps(output, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
