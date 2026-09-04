from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from app.core.config import get_settings
from app.infrastructure.db import SqlAlchemyUnitOfWork
from app.services.weekly_kpi_reports import build_pending_weekly_kpi_artifacts


def _parse_date(value: str | None) -> date | None:
    if value:
        return date.fromisoformat(value)
    return None


def build_artifacts(
    *,
    output_dir: Path,
    week_end: date | None,
    report_ids: list[int] | None,
) -> dict[str, object]:
    with SqlAlchemyUnitOfWork() as unit_of_work:
        if unit_of_work.session is None:
            raise RuntimeError("application Unit of Work did not provide a session")
        return build_pending_weekly_kpi_artifacts(
            unit_of_work.session,
            output_dir=output_dir,
            week_end=week_end,
            report_ids=report_ids,
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build XLSX artifacts for published weekly KPI reports"
    )
    parser.add_argument(
        "--week-end", dest="week_end", help="Optional week end in YYYY-MM-DD format"
    )
    parser.add_argument(
        "--report-id",
        action="append",
        dest="report_ids",
        type=int,
        help="Optional report id filter; can be passed multiple times",
    )
    parser.add_argument(
        "--output-dir",
        default=get_settings().weekly_kpi_artifact_dir,
        help="Artifact root directory",
    )
    args = parser.parse_args()

    result = build_artifacts(
        output_dir=Path(args.output_dir),
        week_end=_parse_date(args.week_end),
        report_ids=args.report_ids,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
