from __future__ import annotations

import argparse
import json
import logging
import sys

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.infrastructure.db.engines import build_engine
from app.services.logistics_onec import sync_warehouse_address_aliases


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync logistics warehouse address aliases from 1C departments."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write warehouse aliases. Default is dry-run.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max 1C department rows to inspect.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable connection debug logs.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    settings = get_settings()
    if not settings.onec_database_url:
        raise SystemExit("ONEC_DATABASE_URL is not configured")

    app_engine = build_engine(settings.database_url, pool_pre_ping=True)
    onec_engine = build_engine(settings.onec_database_url, pool_pre_ping=True)
    with Session(app_engine) as session:
        result = sync_warehouse_address_aliases(
            session,
            onec_engine,
            limit=args.limit,
            dry_run=not args.apply,
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
