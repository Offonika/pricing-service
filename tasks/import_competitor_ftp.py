"""CLI для импорта датированных прайсов конкурентов по FTP."""

import json
import logging
import sys

from app.workers.competitor_ftp import run_competitor_ftp_import


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = run_competitor_ftp_import()
    print(json.dumps(result, ensure_ascii=False))
    exit_code = 0 if (result.get("errors") or 0) == 0 else 1
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
