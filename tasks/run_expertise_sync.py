from __future__ import annotations

import json
import logging
import sys

from app.workers.expertise import run_expertise_bitrix_sync, run_expertise_onec_sync


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    onec_result = run_expertise_onec_sync()
    bitrix_result = run_expertise_bitrix_sync()
    result = {
        "onec_sync": onec_result,
        "bitrix_sync": bitrix_result,
    }
    print(json.dumps(result, ensure_ascii=False))
    sys.exit(0 if not bitrix_result.get("errors") else 1)


if __name__ == "__main__":
    main()
