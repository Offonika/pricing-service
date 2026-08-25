#!/usr/bin/env python3
"""Process the durable pickup-bot outbox and due SLA tasks."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_settings  # noqa: E402
from app.infrastructure.db import session_scope  # noqa: E402
from app.services import pickup_control  # noqa: E402
from app.services import site_order_fulfillment as fulfillment  # noqa: E402
from app.services import site_order_fulfillment_bot as bot  # noqa: E402
from infra.cron import order_fulfillment_sync as fulfillment_sync  # noqa: E402


def build_runtime_apply_enabled_probe(
    *,
    initial_enabled: bool,
    env_file: Path = PROJECT_ROOT / ".env",
) -> Callable[[], bool]:
    if not initial_enabled:
        return lambda: False
    still_enabled = True

    def enabled() -> bool:
        nonlocal still_enabled
        if not still_enabled:
            return False
        still_enabled = bot.runtime_apply_enabled_from_env(
            initial_enabled=True,
            env_file=env_file,
        )
        return still_enabled

    return enabled


def build_runtime_missing_receipt_enabled_probe(
    *,
    initial_enabled: bool,
    env_file: Path = PROJECT_ROOT / ".env",
) -> Callable[[], bool]:
    if not initial_enabled:
        return lambda: False
    still_enabled = True

    def enabled() -> bool:
        nonlocal still_enabled
        if not still_enabled:
            return False
        still_enabled = bot.runtime_feature_enabled_from_env(
            initial_enabled=True,
            env_key=bot.MISSING_RECEIPT_ENABLED_ENV_KEY,
            env_file=env_file,
        )
        return still_enabled

    return enabled


def build_onec_validator() -> Callable[[str], bot.OneCPickupValidation]:
    def validate(order_number: str) -> bot.OneCPickupValidation:
        try:
            settlements = fulfillment_sync.fetch_onec_order_settlements([order_number])
            rtu_signals = fulfillment_sync.query_rtu_signal_by_orders([order_number])
        except Exception:
            return bot.OneCPickupValidation(available=False, evidence="onec_read_error")
        settlement = settlements.get(order_number)
        rtu_signal = rtu_signals.get(order_number) or {}
        if settlement is None:
            return bot.OneCPickupValidation(available=False, evidence="onec_order_not_found")
        debt_conflict = bool(
            not settlement.payment_confirmed
            and (
                settlement.debt_amount is None
                or settlement.debt_amount > Decimal("0.05")
                or (
                    settlement.payment_amount is not None
                    and settlement.payment_amount > Decimal("0.05")
                )
            )
        )
        return bot.OneCPickupValidation(
            available=True,
            assembled=int(rtu_signal.get("assembled_rtu_count") or 0) > 0,
            payment_confirmed=settlement.payment_confirmed,
            debt_conflict=debt_conflict,
            issued_confirmed=int(rtu_signal.get("issued_rtu_count") or 0) > 0,
            return_confirmed=int(rtu_signal.get("returned_rtu_count") or 0) > 0,
            evidence=settlement.evidence,
        )

    return validate


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=50)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = get_settings()
    if not settings.order_fulfillment_bot_enabled:
        print('{"status":"disabled"}')
        return 0
    if not str(settings.order_fulfillment_bitrix_webhook_url or "").strip():
        raise SystemExit("ORDER_FULFILLMENT_BITRIX_WEBHOOK_URL is not configured")
    if not str(settings.order_fulfillment_bot_client_id or "").strip():
        raise SystemExit("ORDER_FULFILLMENT_BOT_CLIENT_ID is not configured")
    client = fulfillment.BitrixChatClient(
        settings.order_fulfillment_bitrix_webhook_url,
        bot_client_id=settings.order_fulfillment_bot_client_id,
    )
    apply_enabled_probe = build_runtime_apply_enabled_probe(
        initial_enabled=settings.order_fulfillment_bot_apply_enabled,
    )
    missing_receipt_enabled_probe = build_runtime_missing_receipt_enabled_probe(
        initial_enabled=settings.order_fulfillment_pickup_missing_receipt_enabled,
    )
    onec_validator = build_onec_validator()
    with session_scope() as session:
        poll_stats: dict[str, object]
        try:
            poll_stats = pickup_control.poll_pickup_control_chats(
                session,
                client=client,
                settings=settings,
                onec_validator=onec_validator,
                external_apply_enabled=apply_enabled_probe(),
            )
        except Exception as exc:  # noqa: BLE001 - outbox must continue after ingest failure.
            session.rollback()
            poll_stats = {"error": type(exc).__name__}
        try:
            missing_receipt_stats: dict[str, object] = (
                pickup_control.enqueue_missing_receipt_followups(
                    session,
                    settings=settings,
                )
            )
        except Exception as exc:  # noqa: BLE001 - outbox must continue.
            session.rollback()
            missing_receipt_stats = {"error": type(exc).__name__}
        try:
            field_stats = bot.reconcile_pickup_case_fields(
                session,
                client=client,
                settings=settings,
                limit=50,
            )
        except Exception as exc:  # noqa: BLE001 - outbox must continue after readback failure.
            session.rollback()
            field_stats = {"error": type(exc).__name__}
        try:
            inventory_stats: dict[str, object] = pickup_control.enqueue_inventory_won_candidates(
                session,
                client=client,
                settings=settings,
                onec_validator=onec_validator,
                limit=50,
            )
        except Exception as exc:  # noqa: BLE001 - outbox must continue.
            session.rollback()
            inventory_stats = {"error": type(exc).__name__}
        sla_result: dict[str, object] = {"created": 0}
        if apply_enabled_probe():
            try:
                sla_result["created"] = bot.enqueue_due_sla_tasks(
                    session,
                    settings=settings,
                )
            except Exception as exc:  # noqa: BLE001 - outbox must continue.
                session.rollback()
                sla_result = {"created": 0, "error": type(exc).__name__}
        stats = bot.process_outbox(
            session,
            client=client,
            settings=settings,
            onec_validator=onec_validator,
            apply_enabled_probe=apply_enabled_probe,
            missing_receipt_enabled_probe=missing_receipt_enabled_probe,
            limit=max(1, min(args.limit, 500)),
        )
    print(
        json.dumps(
            {
                "poll": poll_stats,
                "missing_receipt": missing_receipt_stats,
                "field_sync": field_stats,
                "inventory": inventory_stats,
                "sla": sla_result,
                **stats,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
