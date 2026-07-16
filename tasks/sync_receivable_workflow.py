from __future__ import annotations

import argparse
import json
import sys
from datetime import date

from app.workers.receivable_workflow import run_receivable_workflow_sync


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sync buyer receivables work items and SMS outbox from read models"
    )
    parser.add_argument("--date", dest="as_of", type=_parse_date, help="Business date YYYY-MM-DD")
    parser.add_argument("--force", action="store_true", help="Run even when workflow flag is off")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--plan",
        action="store_true",
        help="Read Bitrix and calculate actions, then roll back local changes",
    )
    mode.add_argument(
        "--dry-run-bitrix",
        action="store_true",
        help="Update local tables only, without Bitrix REST writes",
    )
    parser.add_argument(
        "--bitrix-only",
        action="store_true",
        help="Synchronize Bitrix cards without planning or sending SMS",
    )
    parser.add_argument(
        "--no-close",
        action="store_true",
        help="Do not close cards missing from the current full snapshot",
    )
    parser.add_argument("--counterparty-ref", action="append", default=[])
    parser.add_argument("--limit", type=int)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument(
        "--all-departments",
        action="store_true",
        help="Ignore the configured pilot department scope",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        help="Apply all eligible cards in commits of this size",
    )
    args = parser.parse_args()

    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    if args.offset < 0:
        parser.error("--offset must be non-negative")
    if args.batch_size is not None and args.batch_size < 1:
        parser.error("--batch-size must be at least 1")
    if args.batch_size and not args.bitrix_only:
        parser.error("--batch-size requires --bitrix-only")
    if args.batch_size and (
        args.plan
        or args.dry_run_bitrix
        or args.limit is not None
        or args.offset
        or args.counterparty_ref
    ):
        parser.error(
            "--batch-size cannot be combined with plan, dry-run, limit, offset or explicit refs"
        )

    result = run_receivable_workflow_sync(
        as_of=args.as_of,
        force=args.force or args.plan,
        dry_run_bitrix=args.dry_run_bitrix,
        plan=args.plan,
        bitrix_only=args.bitrix_only,
        allow_closure=not args.no_close,
        counterparty_refs=tuple(args.counterparty_ref),
        limit=args.limit,
        offset=args.offset,
        all_departments=args.all_departments,
        batch_size=args.batch_size,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 1 if result.get("status") == "error" else 0


if __name__ == "__main__":
    sys.exit(main())
