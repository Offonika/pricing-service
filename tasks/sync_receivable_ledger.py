from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path

from app.workers.receivables import run_receivable_ledger_sync


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value)


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync normalized receivable ledger from 1C SQL")
    parser.add_argument("--sql-file", required=True, help="Path to normalized SQL projection")
    parser.add_argument("--snapshot-date", help="Snapshot date in YYYY-MM-DD")
    parser.add_argument(
        "--opening-balance-date",
        help="Optional opening balance date in YYYY-MM-DD for 1C opening layer sync",
    )
    parser.add_argument(
        "--opening-import-path",
        help="Optional normalized opening seed file for 01.01.2025 formula",
    )
    parser.add_argument("--window-start", help="Optional lower bound in ISO datetime format")
    parser.add_argument("--window-end", help="Optional upper bound in ISO datetime format")
    parser.add_argument(
        "--employee-counterparty-ref",
        action="append",
        default=[],
        help="Counterparty ref to mark as employee debt case; may be repeated",
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
    args = parser.parse_args()

    sql_path = Path(args.sql_file)
    if not sql_path.exists():
        raise SystemExit(f"SQL file not found: {sql_path}")

    result = run_receivable_ledger_sync(
        operations_sql=sql_path.read_text(encoding="utf-8"),
        snapshot_date=_parse_date(args.snapshot_date),
        opening_balance_date=_parse_date(args.opening_balance_date),
        opening_import_path=args.opening_import_path,
        window_start=_parse_datetime(args.window_start),
        window_end=_parse_datetime(args.window_end),
        employee_counterparty_refs=tuple(args.employee_counterparty_ref),
        fired_manager_refs=tuple(args.fired_manager_ref),
        replace_existing=args.replace_ledger,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
