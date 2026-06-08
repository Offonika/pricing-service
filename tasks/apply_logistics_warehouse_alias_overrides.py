from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.services.logistics_onec import apply_warehouse_alias_overrides


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Apply confirmed logistics warehouse address aliases."
    )
    parser.add_argument(
        "json_path",
        type=Path,
        help="JSON file with confirmed aliases.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write aliases. Default is dry-run.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    overrides = load_overrides(args.json_path)
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    with Session(engine) as session:
        result = apply_warehouse_alias_overrides(
            session,
            overrides,
            dry_run=not args.apply,
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


def load_overrides(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        payload = payload.get("aliases") or payload.get("items") or [payload]
    if not isinstance(payload, list):
        raise SystemExit("JSON must be an object or a list of alias override objects")
    return [dict(item) for item in payload]


if __name__ == "__main__":
    sys.exit(main())
