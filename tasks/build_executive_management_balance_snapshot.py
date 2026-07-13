from __future__ import annotations

import argparse
import json
from datetime import date

from app.infrastructure.db.session import get_application_session_factory
from app.services.executive_management_balance import (
    build_management_balance_snapshot_command,
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
    parser.add_argument(
        "--trigger",
        choices=("cron", "manual"),
        default="manual",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    session_factory = get_application_session_factory()
    with session_factory() as session:
        result = build_management_balance_snapshot_command(
            session,
            balance_date=args.balance_date,
            view=args.view,
            actor="system:management-balance-snapshot",
            trigger=args.trigger,
        )
    print(
        json.dumps(
            {
                "status": "ok",
                "outcome": result.outcome,
                "version": result.snapshot.version,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
