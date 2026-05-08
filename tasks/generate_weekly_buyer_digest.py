"""CLI для запуска еженедельного обзора новинок смартфонов."""

import json
import logging
import sys

from app.workers.weekly_buyer_digest import run_weekly_buyer_digest_job


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = run_weekly_buyer_digest_job()
    print(json.dumps(result, ensure_ascii=False))
    exit_code = 0 if (result.get("errors") or 0) == 0 else 1
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
