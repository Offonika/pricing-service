from __future__ import annotations

import json
import logging
import sys

from app.workers.expertise import run_expertise_alarm_scan


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = run_expertise_alarm_scan()
    print(json.dumps(result, ensure_ascii=False))
    sys.exit(0 if not result.get("errors") else 1)


if __name__ == "__main__":
    main()
