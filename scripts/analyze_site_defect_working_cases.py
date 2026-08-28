#!/usr/bin/env python3
"""Analyze working site defect cases and optionally write hints/tasks to Bitrix."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.config import get_settings  # noqa: E402
from app.infrastructure.db import session_scope  # noqa: E402
from app.services.site_defect_workflow import analyze_bitrix_working_reclamations  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze Bitrix working site defect cases with archive hints."
    )
    parser.add_argument("--case-id", default=None, help="Bitrix smart-process item id")
    parser.add_argument(
        "--limit", type=int, default=5, help="How many recent working cards to check"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Only print analysis")
    mode.add_argument("--apply", action="store_true", help="Write hints/comments/tasks to Bitrix")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = get_settings()
    with session_scope(read_only=True) as session:
        summary = analyze_bitrix_working_reclamations(
            session,
            settings=settings,
            item_id=args.case_id,
            limit=args.limit,
            apply=bool(args.apply),
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
