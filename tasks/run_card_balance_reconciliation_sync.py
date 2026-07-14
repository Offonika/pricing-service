from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date

from app.workers.card_balance_reconciliation import (
    run_card_balance_bitrix_sync,
    run_card_balance_onec_cashbox_sync,
)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be at least 1")
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync card balance reconciliation from 1C and Bitrix."
    )
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--skip-cashboxes", action="store_true")
    parser.add_argument("--business-date", type=date.fromisoformat, default=None)
    auto_create = parser.add_mutually_exclusive_group()
    auto_create.add_argument("--auto-create", action="store_true", dest="auto_create")
    auto_create.add_argument("--skip-auto-create", action="store_false", dest="auto_create")
    parser.set_defaults(auto_create=None)
    parser.add_argument(
        "--dry-run-auto-create",
        action="store_true",
        help="Preview daily Bitrix item auto-create without creating or updating Bitrix items.",
    )
    workday = parser.add_mutually_exclusive_group()
    workday.add_argument(
        "--require-workday",
        action="store_true",
        dest="require_workday",
        help="Require a store shift fact/plan before creating a daily Bitrix item.",
    )
    workday.add_argument(
        "--ignore-workday",
        action="store_false",
        dest="require_workday",
        help="Temporarily skip the workday check for a controlled pilot run.",
    )
    parser.set_defaults(require_workday=None)
    parser.add_argument(
        "--pilot-cashbox-code",
        action="append",
        default=None,
        help=(
            "Limit daily auto-create to this 1C cashbox code for the current run. "
            "Can be passed multiple times."
        ),
    )
    parser.add_argument(
        "--max-create-count",
        type=_positive_int,
        default=None,
        help="Limit how many daily Bitrix items can be created or planned in this run.",
    )
    ocr = parser.add_mutually_exclusive_group()
    ocr.add_argument(
        "--enable-ocr",
        action="store_true",
        dest="ocr_enabled",
        help="Run screenshot OCR during Bitrix item sync for this run.",
    )
    ocr.add_argument(
        "--skip-ocr",
        action="store_false",
        dest="ocr_enabled",
        help="Skip screenshot OCR for a controlled manual-balance pilot run.",
    )
    parser.set_defaults(ocr_enabled=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result: dict[str, object] = {}
    if not args.skip_cashboxes:
        result["onec_cashboxes"] = run_card_balance_onec_cashbox_sync()
    result["bitrix_sync"] = run_card_balance_bitrix_sync(
        limit=args.limit,
        business_date=args.business_date,
        auto_create_daily=args.auto_create,
        dry_run_auto_create=args.dry_run_auto_create,
        require_workday=args.require_workday,
        pilot_cashbox_codes=args.pilot_cashbox_code,
        ocr_enabled=args.ocr_enabled,
        max_create_count=args.max_create_count,
    )
    print(json.dumps(result, ensure_ascii=False))
    errors = (
        int((result.get("bitrix_sync") or {}).get("errors", 0))
        if isinstance(result.get("bitrix_sync"), dict)
        else 0
    )
    sys.exit(0 if errors == 0 else 1)


if __name__ == "__main__":
    main()
