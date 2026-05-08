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
