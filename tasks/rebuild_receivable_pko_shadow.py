#!/usr/bin/env python3
"""Rebuild the isolated PKO shadow comparison for one receivables snapshot."""

from __future__ import annotations

import argparse
import json
from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.infrastructure.db.engines import build_engine
from app.services.receivable_pko_shadow import (
    PKO_SHADOW_ALGORITHM_VERSION,
    rebuild_receivable_pko_shadow,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, help="Receivables snapshot date: YYYY-MM-DD")
    parser.add_argument(
        "--algorithm-version",
        default=PKO_SHADOW_ALGORITHM_VERSION,
        help="Version label stored with the shadow snapshot.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable summary.")
    return parser.parse_args(argv)


def _json_safe(value: Any) -> Any:
    if isinstance(value, date):
        return value.isoformat()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    snapshot_date = date.fromisoformat(args.date)
    settings = get_settings()
    if not settings.onec_database_url:
        raise RuntimeError("ONEC_DATABASE_URL is required for the PKO shadow rebuild")

    db_engine = build_engine(settings.database_url, pool_pre_ping=True)
    onec_engine = build_engine(
        settings.onec_database_url,
        connect_args={
            "timeout": float(settings.onec_query_timeout_seconds),
            "login_timeout": float(settings.onec_login_timeout_seconds),
        },
        pool_pre_ping=True,
    )
    try:
        with Session(db_engine) as session:
            summary = rebuild_receivable_pko_shadow(
                session,
                onec_engine=onec_engine,
                snapshot_date=snapshot_date,
                algorithm_version=args.algorithm_version,
            )
            session.commit()
    finally:
        onec_engine.dispose()
        db_engine.dispose()

    payload = {key: _json_safe(value) for key, value in summary.items()}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(
            "receivable PKO shadow rebuilt:",
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
