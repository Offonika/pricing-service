from __future__ import annotations

import argparse
import json
from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.services.executive_management_balance import (
    build_and_persist_management_balance_snapshot,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a versioned executive management balance snapshot"
    )
    parser.add_argument("--date", dest="balance_date", type=date.fromisoformat)
    parser.add_argument(
        "--view",
        choices=("operational", "closed"),
        default="operational",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    settings = get_settings()
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    try:
        with Session(engine) as session:
            snapshot = build_and_persist_management_balance_snapshot(
                session,
                balance_date=args.balance_date,
                view=args.view,
                actor="system:management-balance-snapshot",
            )
        print(
            json.dumps(
                {
                    "status": "ok",
                    "month": snapshot.period_month.strftime("%Y-%m"),
                    "balance_date": snapshot.balance_date.isoformat(),
                    "view": snapshot.view_mode,
                    "version": snapshot.version,
                    "source_status": snapshot.source_status,
                    "can_close": not snapshot.validation_errors,
                    "validation_errors": snapshot.validation_errors,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
