from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from app.infrastructure.db.session import get_application_session_factory
from app.services.executive_management_balance import (
    atomic_write_management_balance_components,
    build_management_balance_components_export,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Export historical management-balance components without DB persistence "
            "or opening-equity enrichment"
        )
    )
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    session_factory = get_application_session_factory()
    with session_factory() as session:
        payload = build_management_balance_components_export(
            session,
            balance_date=args.date,
        )
    if not args.dry_run:
        atomic_write_management_balance_components(args.output, payload)
    print(
        json.dumps(
            {
                "status": "dry_run" if args.dry_run else "ok",
                "output": str(args.output),
                "as_of": payload["as_of"],
                "source_hash": payload["source_hash"],
                "totals": payload["totals"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
