from __future__ import annotations

import argparse
import csv
import json
from datetime import date
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.services.manual_matching_feedback import (
    build_manual_matching_feedback_report,
    render_manual_matching_feedback_markdown,
)

DEFAULT_OUTPUT_DIR = Path("reports/manual_matching_feedback")
DATASET_FIELDS = (
    "decision_id",
    "decided_at",
    "label",
    "action",
    "product_id",
    "product_article",
    "product_name",
    "competitor_item_id",
    "competitor",
    "competitor_external_id",
    "competitor_name",
    "item_type",
    "manual_reason",
    "created_by",
    "guardrail_allowed",
    "diagnostic_reasons",
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze manual competitor matching decisions without changing database state."
    )
    parser.add_argument(
        "--as-of",
        help="Include decisions through this date in Europe/Moscow (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="Directory for Markdown, JSON and CSV artifacts.",
    )
    parser.add_argument(
        "--database-url",
        help="Override DATABASE_URL for tests or one-off local runs.",
    )
    parser.add_argument(
        "--sample-limit",
        type=int,
        default=20,
        help="Maximum number of sample rows per diagnostic group.",
    )
    parser.add_argument(
        "--no-files",
        action="store_true",
        help="Do not create artifacts; useful for a read-only console check.",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON report to stdout.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> dict:
    args = parse_args(argv)
    as_of = date.fromisoformat(args.as_of) if args.as_of else None
    database_url = args.database_url or get_settings().database_url
    engine = create_engine(database_url, pool_pre_ping=True)

    with Session(engine) as session:
        report, dataset_rows = build_manual_matching_feedback_report(
            session,
            as_of=as_of,
            sample_limit=args.sample_limit,
        )

    artifacts: dict[str, str] = {}
    if not args.no_files:
        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        stem = f"manual_matching_feedback_{report['as_of']}"
        markdown_path = output_dir / f"{stem}.md"
        json_path = output_dir / f"{stem}.json"
        csv_path = output_dir / f"{stem}.csv"

        markdown_path.write_text(render_manual_matching_feedback_markdown(report), encoding="utf-8")
        json_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=DATASET_FIELDS)
            writer.writeheader()
            writer.writerows(dataset_rows)
        artifacts = {
            "markdown": str(markdown_path),
            "json": str(json_path),
            "csv": str(csv_path),
        }

    report["artifacts"] = artifacts
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return report


if __name__ == "__main__":
    main()
