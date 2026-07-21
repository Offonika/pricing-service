from __future__ import annotations

import argparse
import json

from app.workers.expertise import run_expertise_completion_outcome_backfill


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill expertise completion outcomes")
    parser.add_argument("--dry-run", action="store_true", help="Count affected cases only")
    args = parser.parse_args()
    result = run_expertise_completion_outcome_backfill(dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
