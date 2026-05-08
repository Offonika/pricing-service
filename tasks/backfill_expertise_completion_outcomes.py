from __future__ import annotations

import json

from app.workers.expertise import run_expertise_completion_outcome_backfill


def main() -> None:
    result = run_expertise_completion_outcome_backfill()
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
