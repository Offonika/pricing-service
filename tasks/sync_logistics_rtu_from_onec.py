from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import date, timedelta

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.infrastructure.db.engines import build_engine
from app.services.logistics_onec import sync_ready_rtu_units

logger = logging.getLogger(__name__)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
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
    parser.add_argument(
        "--limit",
        type=_positive_int,
        default=500,
        help="1C page size. All matching pages are processed.",
    )
    parser.add_argument(
        "--site-order-number",
        default=None,
        help="Only process RTU rows linked to this site order number.",
    )
    parser.add_argument(
        "--rtu-external-id",
        default=None,
        help="Only process the RTU with this SQL _IDRRef value.",
    )
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
    return parser.parse_args(argv)


def _merge_report(aggregate: dict, page: dict) -> None:
    aggregate["pages"] += 1
    for key, value in page.items():
        if key == "dry_run":
            continue
        if key == "by_reason":
            totals = aggregate.setdefault("by_reason", {})
            for reason, count in value.items():
                totals[reason] = totals.get(reason, 0) + int(count)
            continue
        if isinstance(value, int) and not isinstance(value, bool):
            aggregate[key] = aggregate.get(key, 0) + value


def sync_all_pages(
    session: Session,
    onec_engine,
    *,
    date_from: date | None,
    page_size: int,
    dry_run: bool,
    external_carrier_flow: bool,
    site_order_number: str | None,
    rtu_external_id: str | None,
) -> dict:
    aggregate: dict = {
        "dry_run": dry_run,
        "pages": 0,
        "by_reason": {},
    }
    offset = 0
    while True:
        page = sync_ready_rtu_units(
            session,
            onec_engine,
            date_from=date_from,
            limit=page_size,
            offset=offset,
            site_order_number=site_order_number,
            rtu_external_id=rtu_external_id,
            dry_run=dry_run,
            external_carrier_flow=external_carrier_flow,
        )
        _merge_report(aggregate, page)
        fetched = int(page.get("fetched") or 0)
        if fetched < page_size:
            break
        offset += fetched
    aggregate["page_size"] = page_size
    return aggregate


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    settings = get_settings()
    if not settings.onec_database_url:
        raise SystemExit("ONEC_DATABASE_URL is not configured")

    date_from = args.date_from
    if date_from is None and not args.site_order_number and not args.rtu_external_id:
        date_from = date.today() - timedelta(days=14)

    started_at = time.monotonic()
    app_engine = build_engine(settings.database_url, pool_pre_ping=True)
    onec_engine = build_engine(settings.onec_database_url, pool_pre_ping=True)
    try:
        with Session(app_engine) as session:
            result = sync_all_pages(
                session,
                onec_engine,
                date_from=date_from,
                page_size=args.limit,
                dry_run=not args.apply,
                external_carrier_flow=args.external_carriers,
                site_order_number=args.site_order_number,
                rtu_external_id=args.rtu_external_id,
            )
    finally:
        app_engine.dispose()
        onec_engine.dispose()
    result["duration_seconds"] = round(time.monotonic() - started_at, 3)
    result["date_from"] = date_from.isoformat() if date_from else None
    result["site_order_number"] = args.site_order_number
    result["rtu_external_id"] = args.rtu_external_id
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
