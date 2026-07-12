#!/usr/bin/env python3
"""Rebuild cached receivables workplace data for one snapshot date."""

from __future__ import annotations

import argparse
import json
from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.infrastructure.db.engines import build_engine
from app.services.counterparty_folder_recommendations import (
    build_counterparty_folder_recommendations,
)
from app.services.receivable_workplace_cache import (
    cache_folder_recommendation_report,
    rebuild_open_debt_cache,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, help="Receivables snapshot date: YYYY-MM-DD")
    parser.add_argument(
        "--skip-folders",
        action="store_true",
        help="Only rebuild open debt documents, without folder recommendations.",
    )
    parser.add_argument(
        "--include-onec-open-debt",
        action="store_true",
        help="Also enrich open-debt documents from 1C while rebuilding the workplace cache.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable summary.")
    return parser.parse_args()


def _engine(url: str):
    return build_engine(url, pool_pre_ping=True)


def main() -> int:
    args = _parse_args()
    snapshot_date = date.fromisoformat(args.date)
    settings = get_settings()
    db_engine = _engine(settings.database_url)
    onec_engine = None
    if settings.onec_database_url:
        onec_engine = build_engine(
            settings.onec_database_url,
            connect_args={
                "timeout": float(settings.onec_query_timeout_seconds),
                "login_timeout": float(settings.onec_login_timeout_seconds),
            },
            pool_pre_ping=True,
        )

    summary: dict[str, Any] = {"snapshot_date": snapshot_date.isoformat()}
    try:
        with Session(db_engine) as session:
            open_debt_summary = rebuild_open_debt_cache(
                session,
                snapshot_date=snapshot_date,
                onec_engine=onec_engine,
                include_onec_enrichment=bool(args.include_onec_open_debt),
            )
            summary["open_debt"] = {
                **open_debt_summary,
                "snapshot_date": snapshot_date.isoformat(),
                "computed_at": open_debt_summary["computed_at"].isoformat(),
            }
            if not args.skip_folders:
                if onec_engine is None:
                    raise RuntimeError("ONEC_DATABASE_URL is required for folder cache rebuild")
                report = build_counterparty_folder_recommendations(
                    session,
                    onec_engine=onec_engine,
                    snapshot_date=snapshot_date,
                    limit=None,
                    status=None,
                    candidate_limit=None,
                    snapshot_department_refs=None,
                )
                row = cache_folder_recommendation_report(session, report=report)
                summary["folder_recommendations"] = {
                    "report_revision": row.report_revision,
                    "payload_count": len(row.payload or []),
                    "computed_at": row.computed_at.isoformat(),
                }
            session.commit()
    finally:
        db_engine.dispose()
        if onec_engine is not None:
            onec_engine.dispose()

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(
            "receivable workplace cache rebuilt:",
            json.dumps(summary, ensure_ascii=False, sort_keys=True, default=str),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
