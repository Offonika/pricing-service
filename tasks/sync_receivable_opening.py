from __future__ import annotations

import argparse
import json
from datetime import date

from app.workers.receivables import run_receivable_opening_sync


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync receivable opening seed/layers")
    parser.add_argument(
        "--opening-balance-date",
        required=True,
        help="Opening balance date in YYYY-MM-DD",
    )
    parser.add_argument(
        "--opening-import-path",
        help="Optional normalized opening seed file for 01.01.2025 formula",
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
        help="Opening layer name to run; may be repeated",
    )
    parser.add_argument(
        "--replace-ledger",
        action="store_true",
        help="Delete existing receivable ledger and derived read-models before sync",
    )
    args = parser.parse_args()

    result = run_receivable_opening_sync(
        opening_balance_date=_parse_date(args.opening_balance_date),
        opening_import_path=args.opening_import_path,
        employee_counterparty_refs=tuple(args.employee_counterparty_ref),
        replace_existing=args.replace_ledger,
        layer_names=tuple(args.layer) or None,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
