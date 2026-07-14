from __future__ import annotations

import json
import logging
import sys

from app.workers.expertise import run_expertise_bitrix_sync, run_expertise_onec_sync


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    onec_result = run_expertise_onec_sync()
    bitrix_result = run_expertise_bitrix_sync(only_failed=True)
    result = {
        "onec_sync": onec_result,
        "bitrix_retry_sync": bitrix_result,
    }
    print(json.dumps(result, ensure_ascii=False))
    scanned = int(bitrix_result.get("scanned") or 0)
    synced = int(bitrix_result.get("synced") or 0)
    errors = int(bitrix_result.get("errors") or 0)
    fatal_bitrix_failure = scanned > 0 and errors > 0 and synced == 0
    sys.exit(1 if fatal_bitrix_failure else 0)


if __name__ == "__main__":
    main()
