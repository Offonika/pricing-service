from __future__ import annotations

import argparse
import json
from datetime import UTC, date, datetime, timedelta

from app.infrastructure.db.engines import get_application_engine, get_onec_engine
from app.services.onec_stock_availability import (
    DEFAULT_HISTORY_DAYS,
    DEFAULT_RETENTION_DAYS,
    month_start,
    sync_onec_stock_availability,
)


def main() -> int:
    args = _parse_args()
    date_to = args.date_to or (date.today() - timedelta(days=1))
    if args.mode == "nightly":
        previous_month = month_start(date_to) - timedelta(days=1)
        date_from = month_start(previous_month)
    else:
        date_from = args.date_from or (date_to - timedelta(days=DEFAULT_HISTORY_DAYS - 1))
    if date_from > date_to:
        raise SystemExit("date_from must not exceed date_to")

    run_key = args.run_key or (
        f"onec-stock-availability:{args.mode}:" f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    )
    onec_engine = get_onec_engine()
    application_engine = get_application_engine()
    try:
        result = sync_onec_stock_availability(
            onec_engine,
            application_engine,
            date_from=date_from,
            date_to=date_to,
            run_key=run_key,
            retention_days=args.retention_days,
        )
    finally:
        onec_engine.dispose()
        application_engine.dispose()

    payload = {
        "status": "ready",
        "mode": args.mode,
        "run_key": run_key,
        "run_ids": list(result.run_ids),
        "range_start": result.range_start.isoformat(),
        "range_end": result.range_end.isoformat(),
        "opening_rows": result.opening_rows,
        "movement_rows": result.movement_rows,
        "day_delta_rows": result.day_delta_rows,
        "interval_rows": result.interval_rows,
        "removed_rows": result.removed_rows,
        "retention_days": args.retention_days,
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build the read-only daily 1C stock availability cache. "
            "Backfill/weekly modes rebuild 180 days; nightly rebuilds current "
            "and previous months to catch reposted documents."
        )
    )
    parser.add_argument(
        "--mode",
        choices=("backfill", "nightly", "weekly"),
        default="nightly",
    )
    parser.add_argument("--date-from", type=date.fromisoformat)
    parser.add_argument("--date-to", type=date.fromisoformat)
    parser.add_argument("--run-key")
    parser.add_argument("--retention-days", type=int, default=DEFAULT_RETENTION_DAYS)
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())
