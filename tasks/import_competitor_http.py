"""CLI для импорта датированных прайсов конкурентов по HTTPS."""

import json
import logging
import sys

from app.workers.competitor_http import run_competitor_http_import


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = run_competitor_http_import()
    print(json.dumps(result, ensure_ascii=False))
    sys.exit(0 if (result.get("errors") or 0) == 0 else 1)


if __name__ == "__main__":
    main()
