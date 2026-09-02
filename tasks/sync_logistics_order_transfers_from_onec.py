from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.infrastructure.db.engines import build_engine
from app.services.logistics_onec import sync_order_transfer_rows

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_SQL = PROJECT_ROOT / "sql/logistics_order_transfer_v1.sql"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read ORDER_TRANSFER_V1 plans and transfers from 1C."
    )
    parser.add_argument("--apply", action="store_true", help="Write the normalized snapshot.")
    parser.add_argument("--date-from", type=date.fromisoformat, default=None)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--source-sql-file", type=Path, default=DEFAULT_SOURCE_SQL)
    parser.add_argument("--verbose", action="store_true")
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
    if not args.source_sql_file.is_file():
        raise SystemExit(f"Source SQL file is missing: {args.source_sql_file}")
    source_query = args.source_sql_file.read_text(encoding="utf-8")

    app_engine = build_engine(settings.database_url, pool_pre_ping=True)
    onec_engine = build_engine(settings.onec_database_url, pool_pre_ping=True)
    with Session(app_engine) as session:
        result = sync_order_transfer_rows(
            session,
            onec_engine,
            source_query=source_query,
            date_from=args.date_from,
            limit=args.limit,
            dry_run=not args.apply,
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
