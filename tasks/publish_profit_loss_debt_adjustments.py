from __future__ import annotations

import argparse
import json
from calendar import monthrange
from datetime import date
from typing import Sequence

from sqlalchemy.orm import Session

from app.infrastructure.db.engines import (
    build_onec_engine_from_settings,
    get_application_engine,
)
from app.services.profit_loss_debt_adjustments import (
    classify_debt_writeoff_rows,
    fetch_onec_debt_writeoff_rows,
    publish_debt_writeoff_batch,
)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    period_start, period_end = _month_bounds(args.month)
    onec_engine = build_onec_engine_from_settings()
    try:
        rows = fetch_onec_debt_writeoff_rows(
            onec_engine,
            period_start=period_start,
            period_end=period_end,
        )
    finally:
        onec_engine.dispose()

    batch = classify_debt_writeoff_rows(rows)
    result = batch.publication_payload(period_start=period_start, period_end=period_end)
    result["source_status"] = batch.source_status
    result["mode"] = "publish" if args.publish else "dry-run"

    if args.publish:
        with Session(get_application_engine()) as session:
            publish_debt_writeoff_batch(
                session,
                batch=batch,
                period_start=period_start,
                period_end=period_end,
            )
            session.commit()

    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def _month_bounds(value: str) -> tuple[date, date]:
    try:
        year_text, month_text = value.split("-", maxsplit=1)
        year = int(year_text)
        month = int(month_text)
        period_start = date(year, month, 1)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("month must use YYYY-MM format") from exc
    return period_start, date(year, month, monthrange(year, month)[1])


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Publish classified 1C debt write-offs for the management P&L."
    )
    parser.add_argument("--month", required=True, help="Publication month in YYYY-MM format")
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Write the classified monthly snapshot to pricing-service; default is dry-run",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
