"""CLI для ежедневного контроля схемы Розница -> Возврат -> Не розница."""

import json
import logging
import sys

from app.workers.return_scheme import run_return_scheme_job


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = run_return_scheme_job()
    print(json.dumps(result, ensure_ascii=False))
    sys.exit(0 if not result.get("errors") else 1)


if __name__ == "__main__":
    main()
