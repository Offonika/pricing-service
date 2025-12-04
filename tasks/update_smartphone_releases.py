"""CLI для запуска пайплайна импорта новинок смартфонов."""

import json
import logging
import sys

from app.workers.smartphone_releases import run_smartphone_release_job


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = run_smartphone_release_job()
    print(json.dumps(result, ensure_ascii=False))
    sys.exit(0 if not result.get("errors") else 1)


if __name__ == "__main__":
    main()
