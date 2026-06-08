from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.services.logistics_onec import sync_ready_rtu_units

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync ready 1C RTU documents into logistics units."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write logistics units/manual reviews. Default is dry-run.",
    )
    parser.add_argument(
        "--date-from",
        type=date.fromisoformat,
        default=None,
        help="Only read RTU documents from this date, YYYY-MM-DD.",
    )
    parser.add_argument("--limit", type=int, default=500, help="Max 1C rows to inspect.")
    parser.add_argument(
        "--external-carriers",
        action="store_true",
        help=(
            "For external delivery methods, create RTU units and mark them "
            "with_external_carrier instead of leaving manual review open."
        ),
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

    app_engine = create_engine(settings.database_url, pool_pre_ping=True)
    onec_engine = create_engine(settings.onec_database_url, pool_pre_ping=True)
    with Session(app_engine) as session:
        result = sync_ready_rtu_units(
            session,
            onec_engine,
            date_from=args.date_from,
            limit=args.limit,
            dry_run=not args.apply,
            external_carrier_flow=args.external_carriers,
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
