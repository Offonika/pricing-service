from __future__ import annotations

import argparse
import json
import logging
import sys

from app.workers.bank_payments import run_bank_payments_bitrix_disk_sync


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync bank payment statements from Bitrix Disk folders."
    )
    parser.add_argument("--limit", type=int, default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = run_bank_payments_bitrix_disk_sync(limit=args.limit)
    print(json.dumps(result, ensure_ascii=False))
    sys.exit(0 if int(result.get("errors") or 0) == 0 else 1)


if __name__ == "__main__":
    main()
