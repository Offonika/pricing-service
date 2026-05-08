from __future__ import annotations

import argparse
import json
from datetime import date, timedelta

from app.workers.receivables import run_receivable_read_model_rebuild


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _iter_dates(date_from: date, date_to: date):
    current_date = date_from
    while current_date <= date_to:
        yield current_date
        current_date += timedelta(days=1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rebuild receivable snapshots/cases for each date in a range from ledger"
    )
    parser.add_argument("--date-from", required=True, help="Range start in YYYY-MM-DD")
    parser.add_argument("--date-to", required=True, help="Range end in YYYY-MM-DD")
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

    date_from = _parse_date(args.date_from)
    date_to = _parse_date(args.date_to)
    if date_to < date_from:
        raise SystemExit("date_to must be greater than or equal to date_from")

    result: dict[str, object] = {
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "rebuilds": {},
    }
    rebuilds: dict[str, object] = {}
    for snapshot_date in _iter_dates(date_from, date_to):
        rebuilds[snapshot_date.isoformat()] = run_receivable_read_model_rebuild(
            snapshot_date=snapshot_date,
            employee_counterparty_refs=tuple(args.employee_counterparty_ref),
            fired_manager_refs=tuple(args.fired_manager_ref),
        )

    result["rebuilds"] = rebuilds
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
