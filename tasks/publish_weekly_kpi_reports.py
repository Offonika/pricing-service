from __future__ import annotations

import argparse
import json
from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.services.weekly_kpi_reports import last_completed_week_end, publish_weekly_kpi_reports


def _parse_date(value: str | None) -> date:
    if value:
        return date.fromisoformat(value)
    return last_completed_week_end()


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish eligible weekly KPI report drafts")
    parser.add_argument("--week-end", dest="week_end", help="Week end in YYYY-MM-DD format")
    parser.add_argument(
        "--report-key",
        action="append",
        dest="report_keys",
        help="Optional report_key filter; can be passed multiple times",
    )
    args = parser.parse_args()

    week_end = _parse_date(args.week_end)
    engine = create_engine(get_settings().database_url)
    with Session(engine) as session:
        result = publish_weekly_kpi_reports(
            session,
            week_end=week_end,
            report_keys=args.report_keys,
        )
        session.commit()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
