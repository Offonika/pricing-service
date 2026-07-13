#!/usr/bin/env python3
"""Import old Bitrix chat69465 comment-store export into the site defect archive."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy.orm import Session

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.config import get_settings  # noqa: E402
from app.infrastructure.db.engines import build_engine  # noqa: E402
from app.services.site_defect_archive import import_archive_export  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import old Bitrix site defect chat archive into pricing-service."
    )
    parser.add_argument("--source", required=True, help="Folder with comments-store-raw.json")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Parse and print counters only")
    mode.add_argument("--apply", action="store_true", help="Write/update local DB index")
    parser.add_argument(
        "--apply-bitrix", action="store_true", help="Also sync Disk folders and CRM items"
    )
    parser.add_argument("--limit", type=int, default=None, help="Limit number of source posts")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    dry_run = not args.apply
    if args.apply_bitrix and dry_run:
        raise SystemExit("--apply-bitrix can be used only together with --apply")
    settings = get_settings()
    engine = build_engine(settings.database_url)
    with Session(engine) as session:
        summary = import_archive_export(
            session,
            args.source,
            dry_run=dry_run,
            limit=args.limit,
            apply_bitrix=args.apply_bitrix,
            settings=settings,
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
