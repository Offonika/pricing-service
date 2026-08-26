#!/usr/bin/env python3
"""Reparse unresolved pickup inventory as append-only revisions."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_settings  # noqa: E402
from app.infrastructure.db import session_scope  # noqa: E402
from app.services import pickup_inventory  # noqa: E402
from scripts.backfill_order_fulfillment_chats import (  # noqa: E402
    configure_runtime_environment,
    ensure_raw_backfill_schema,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Persist new revisions. Default executes the same logic in a rolled-back savepoint.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    configure_runtime_environment(require_database=True)
    settings = get_settings()
    if not settings.order_fulfillment_pickup_warehouse_aliases:
        raise SystemExit("ORDER_FULFILLMENT_PICKUP_WAREHOUSE_ALIASES is not configured")
    with session_scope() as session:
        ensure_raw_backfill_schema(session)
        savepoint = None if args.apply else session.begin_nested()
        stats = pickup_inventory.reprocess_manual_inventory_submissions(
            session,
            pickup_aliases=settings.order_fulfillment_pickup_warehouse_aliases,
            limit=args.limit,
        )
        session.flush()
        if savepoint is not None:
            savepoint.rollback()
            session.expire_all()
    print(
        json.dumps(
            {"mode": "apply" if args.apply else "dry_run", **stats},
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
