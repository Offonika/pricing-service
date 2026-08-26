from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from app.infrastructure.db import session_scope
from app.services.manual_matching_control import (
    build_manual_matching_control_report,
    render_manual_matching_markdown,
    report_date_today,
)

DEFAULT_OUTPUT_DIR = Path("reports/manual_matching_control")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build daily manual competitor matching control report."
    )
    parser.add_argument(
        "--date",
        dest="report_date",
        help="Report date in YYYY-MM-DD; defaults to today in Europe/Moscow.",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="Directory for Markdown report artifacts.",
    )
    parser.add_argument(
        "--database-url",
        help="Override DATABASE_URL for tests or one-off local runs.",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON report to stdout.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> dict:
    args = parse_args(argv)
    target_date = date.fromisoformat(args.report_date) if args.report_date else report_date_today()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    with session_scope(database_url=args.database_url, read_only=True) as session:
        report = build_manual_matching_control_report(session, report_date=target_date)

    report_path = output_dir / f"{target_date.isoformat()}.md"
    report_path.write_text(render_manual_matching_markdown(report), encoding="utf-8")
    report["report_path"] = str(report_path)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return report


if __name__ == "__main__":
    main()
