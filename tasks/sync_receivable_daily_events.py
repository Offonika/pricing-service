from __future__ import annotations

import argparse
import json
from datetime import date, datetime

from app.workers.receivables import run_receivable_daily_events_sync


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value)


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync layered receivable daily events from 1C")
    parser.add_argument("--snapshot-date", help="Snapshot date in YYYY-MM-DD")
    parser.add_argument("--window-start", help="Optional lower bound in ISO datetime format")
    parser.add_argument("--window-end", help="Optional upper bound in ISO datetime format")
    parser.add_argument(
        "--window-days",
        type=int,
        default=1,
        help=(
            "When --snapshot-date is used without explicit window bounds, "
            "sync this many calendar days ending on snapshot_date"
        ),
    )
    parser.add_argument(
        "--employee-counterparty-ref",
        action="append",
        default=[],
        help="Counterparty ref to mark as employee debt case; may be repeated",
    )
    parser.add_argument(
        "--layer",
        action="append",
        default=[],
        help="Daily layer name to run; may be repeated",
    )
    parser.add_argument(
        "--replace-ledger",
        action="store_true",
        help="Delete existing receivable ledger and derived read-models before sync",
    )
    args = parser.parse_args()

    result = run_receivable_daily_events_sync(
        snapshot_date=_parse_date(args.snapshot_date),
        window_start=_parse_datetime(args.window_start),
        window_end=_parse_datetime(args.window_end),
        window_days=args.window_days,
        employee_counterparty_refs=tuple(args.employee_counterparty_ref),
        replace_existing=args.replace_ledger,
        layer_names=tuple(args.layer) or None,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
