from __future__ import annotations

import argparse
import json
from datetime import date

from app.workers.receivables import run_receivable_history_backfill


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill layered receivable history from 1C")
    parser.add_argument("--date-from", required=True, help="Backfill start date in YYYY-MM-DD")
    parser.add_argument("--date-to", required=True, help="Backfill end date in YYYY-MM-DD")
    parser.add_argument(
        "--opening-balance-date",
        help="Optional opening balance date in YYYY-MM-DD for 1C opening layer sync",
    )
    parser.add_argument(
        "--opening-import-path",
        help="Optional normalized opening seed file for 01.01.2025 formula",
    )
    parser.add_argument(
        "--rebuild-snapshot-date",
        action="append",
        default=[],
        help="Snapshot date in YYYY-MM-DD to rebuild after event backfill; may be repeated",
    )
    parser.add_argument(
        "--employee-counterparty-ref",
        action="append",
        default=[],
        help="Counterparty ref to mark as employee debt case; may be repeated",
    )
    parser.add_argument(
        "--daily-layer",
        action="append",
        default=[],
        help="Daily layer name for backfill; may be repeated",
    )
    parser.add_argument(
        "--fired-manager-ref",
        action="append",
        default=[],
        help="Manager ref to mark as fired-manager debt case; may be repeated",
    )
    parser.add_argument(
        "--replace-ledger",
        action="store_true",
        help="Delete existing receivable ledger and derived read-models before sync",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate the requested range and print the write plan without DB changes",
    )
    args = parser.parse_args()

    if args.dry_run:
        result = {
            "dry_run": True,
            "date_from": args.date_from,
            "date_to": args.date_to,
            "opening_balance_date": args.opening_balance_date,
            "rebuild_snapshot_dates": args.rebuild_snapshot_date,
            "daily_layers": args.daily_layer,
            "replace_existing": args.replace_ledger,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    result = run_receivable_history_backfill(
        date_from=_parse_date(args.date_from),
        date_to=_parse_date(args.date_to),
        opening_balance_date=(
            _parse_date(args.opening_balance_date) if args.opening_balance_date else None
        ),
        opening_import_path=args.opening_import_path,
        rebuild_snapshot_dates=tuple(_parse_date(value) for value in args.rebuild_snapshot_date),
        employee_counterparty_refs=tuple(args.employee_counterparty_ref),
        daily_layer_names=tuple(args.daily_layer) or None,
        fired_manager_refs=tuple(args.fired_manager_ref),
        replace_existing=args.replace_ledger,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
