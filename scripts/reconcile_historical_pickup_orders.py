#!/usr/bin/env python3
"""Build or enqueue an explicitly approved batch of historical pickup decisions."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_settings  # noqa: E402
from app.infrastructure.db import session_scope  # noqa: E402
from app.services import pickup_history  # noqa: E402
from app.services import site_order_fulfillment as fulfillment  # noqa: E402
from scripts.process_order_fulfillment_bot_outbox import build_onec_validator  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument(
        "--batch-size",
        type=int,
        default=pickup_history.MAX_APPROVED_BATCH_SIZE,
    )
    parser.add_argument(
        "--queue",
        choices=(
            pickup_history.QUEUE_WON,
            pickup_history.QUEUE_PRESENT,
            pickup_history.QUEUE_LOSE,
        ),
        default=None,
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--approved-batch-id", default=None)
    return parser.parse_args()


def select_batch(
    rows: list[pickup_history.HistoricalPickupAssessment],
    *,
    queue: str | None,
    batch_size: int,
) -> list[pickup_history.HistoricalPickupAssessment]:
    eligible = [
        row
        for row in rows
        if row.queue
        in {
            pickup_history.QUEUE_WON,
            pickup_history.QUEUE_PRESENT,
            pickup_history.QUEUE_LOSE,
        }
        and (queue is None or row.queue == queue)
        and row.current_stage != row.target_stage
    ]
    return eligible[:batch_size]


def main() -> int:
    args = parse_args()
    if not 1 <= args.batch_size <= pickup_history.MAX_APPROVED_BATCH_SIZE:
        raise SystemExit("--batch-size must be between 1 and 20")
    if args.apply and not args.approved_batch_id:
        raise SystemExit("--apply requires --approved-batch-id from a fresh dry-run")
    settings = get_settings()
    webhook_url = fulfillment._clean_string(  # noqa: SLF001
        settings.order_fulfillment_bitrix_webhook_url
    )
    if not webhook_url:
        raise SystemExit("ORDER_FULFILLMENT_BITRIX_WEBHOOK_URL is not configured")
    if args.apply and not settings.order_fulfillment_bot_apply_enabled:
        raise SystemExit("ORDER_FULFILLMENT_BOT_APPLY_ENABLED must be true")
    client = fulfillment.BitrixChatClient(
        webhook_url,
        bot_client_id=settings.order_fulfillment_bot_client_id,
    )
    with session_scope() as session:
        rows = pickup_history.assess_historical_pickup_cases(
            session,
            client=client,
            settings=settings,
            onec_validator=build_onec_validator(),
            limit=args.limit,
        )
        batch = select_batch(rows, queue=args.queue, batch_size=args.batch_size)
        batch_id = pickup_history.approved_batch_id(batch) if batch else None
        result: dict[str, object] = {
            "mode": "apply" if args.apply else "dry-run",
            "assessed": len(rows),
            "by_queue": dict(Counter(row.queue for row in rows)),
            "batch_id": batch_id,
            "batch": [row.as_dict() for row in batch],
        }
        if args.apply:
            result["enqueue"] = pickup_history.enqueue_approved_batch(
                session,
                rows=batch,
                approved_id=args.approved_batch_id,
                settings=settings,
            )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
