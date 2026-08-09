from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from app.infrastructure.db import get_application_session_factory
from app.services.customer_price_type_review_batches import (
    DEFAULT_BATCH_KEY,
    import_review_batch,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate or import the versioned 82-card customer price-type review batch."
    )
    parser.add_argument("--working-bronze-csv", type=Path, required=True)
    parser.add_argument("--review-queue-csv", type=Path, required=True)
    parser.add_argument("--batch-key", default=DEFAULT_BATCH_KEY)
    parser.add_argument(
        "--label",
        default="Проверка рабочих договоров — июль 2026",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Persist the batch in pricing-service; omitted means validation only.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    with get_application_session_factory()() as session:
        result = import_review_batch(
            session,
            working_bronze_csv=args.working_bronze_csv,
            review_queue_csv=args.review_queue_csv,
            batch_key=args.batch_key,
            label=args.label,
            apply=args.apply,
        )
    print(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
