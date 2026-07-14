from __future__ import annotations

import argparse
import json
from datetime import date

from app.workers.receivable_workflow import run_receivable_workflow_sync


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync buyer receivables work items and SMS outbox from read models"
    )
    parser.add_argument("--date", dest="as_of", type=_parse_date, help="Business date YYYY-MM-DD")
    parser.add_argument("--force", action="store_true", help="Run even when workflow flag is off")
    parser.add_argument(
        "--dry-run-bitrix",
        action="store_true",
        help="Update local tables only, without Bitrix REST writes",
    )
    args = parser.parse_args()

    result = run_receivable_workflow_sync(
        as_of=args.as_of,
        force=args.force,
        dry_run_bitrix=args.dry_run_bitrix,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
