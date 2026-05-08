from __future__ import annotations

import json

from app.workers.counterparty_duplicates import run_counterparty_duplicate_job


def main() -> None:
    result = run_counterparty_duplicate_job()
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
