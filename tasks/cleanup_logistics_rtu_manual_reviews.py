from __future__ import annotations

import argparse
import json
import sys

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.infrastructure.db.engines import build_engine
from app.services.logistics_onec import cleanup_legacy_rtu_manual_review_noise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Resolve legacy RTU manual-review noise with an audit marker."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Resolve matching rows. Default is dry-run.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    engine = build_engine(get_settings().database_url, pool_pre_ping=True)
    with Session(engine) as session:
        result = cleanup_legacy_rtu_manual_review_noise(
            session,
            dry_run=not args.apply,
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
