from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.services.executive_service_accruals import sync_service_accruals


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync recurring-service accruals")
    parser.add_argument("--date", type=date.fromisoformat, default=date.today())
    parser.add_argument("--source", type=Path)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    try:
        with Session(engine) as session:
            result = sync_service_accruals(
                session,
                as_of=args.date,
                actor="system:service-accrual-sync",
                source_path=args.source,
            )
        print(json.dumps({"status": "ok", **result}, ensure_ascii=False, indent=2))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
