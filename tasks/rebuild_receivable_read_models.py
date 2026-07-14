from __future__ import annotations

import argparse
import json
from datetime import date

from app.workers.receivables import run_receivable_read_model_rebuild


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rebuild receivable read-models from receivable_ledger_event"
    )
    parser.add_argument("--snapshot-date", required=True, help="Snapshot date in YYYY-MM-DD")
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
    args = parser.parse_args()

    result = run_receivable_read_model_rebuild(
        snapshot_date=_parse_date(args.snapshot_date),
        employee_counterparty_refs=tuple(args.employee_counterparty_ref),
        fired_manager_refs=tuple(args.fired_manager_ref),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
