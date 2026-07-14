from __future__ import annotations

import argparse
import json
import time
from datetime import date, timedelta

from app.workers.onec_sales_kpi import run_onec_sales_kpi_sync


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _daterange_chunks(
    *, date_from: date, date_to: date, batch_days: int
) -> list[tuple[date, date]]:
    chunks: list[tuple[date, date]] = []
    current = date_from
    step = timedelta(days=batch_days - 1)
    while current <= date_to:
        chunk_to = min(current + step, date_to)
        chunks.append((current, chunk_to))
        current = chunk_to + timedelta(days=1)
    return chunks


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync daily sales KPI from 1C into app DB")
    parser.add_argument("--date-from", required=True, help="Lower date bound in YYYY-MM-DD")
    parser.add_argument("--date-to", required=True, help="Upper date bound in YYYY-MM-DD")
    parser.add_argument(
        "--batch-days",
        type=int,
        default=0,
        help="Split sync into batches of N days to reduce load on 1C",
    )
    parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=0.0,
        help="Sleep between batches",
    )
    args = parser.parse_args()

    date_from = _parse_date(args.date_from)
    date_to = _parse_date(args.date_to)

    if args.batch_days and args.batch_days > 0:
        chunks = _daterange_chunks(
            date_from=date_from,
            date_to=date_to,
            batch_days=args.batch_days,
        )
        summary = {
            "mode": "batched",
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "batch_days": args.batch_days,
            "sleep_seconds": args.sleep_seconds,
            "batches": [],
            "deleted": 0,
            "inserted": 0,
            "fetched": 0,
        }
        for index, (chunk_from, chunk_to) in enumerate(chunks, start=1):
            result = run_onec_sales_kpi_sync(
                date_from=chunk_from,
                date_to=chunk_to,
            )
            result["batch_index"] = index
            result["batch_count"] = len(chunks)
            summary["batches"].append(result)
            summary["deleted"] += int(result["deleted"])
            summary["inserted"] += int(result["inserted"])
            summary["fetched"] += int(result["fetched"])
            print(json.dumps(result, ensure_ascii=False), flush=True)
            if args.sleep_seconds > 0 and index < len(chunks):
                time.sleep(args.sleep_seconds)

        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return

    result = run_onec_sales_kpi_sync(date_from=date_from, date_to=date_to)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
