from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from typing import Any

from app.workers.staffing import run_staffing_sync


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _load_json_array(path_value: str | None) -> list[dict[str, Any]]:
    if not path_value:
        return []
    path = Path(path_value)
    if not path.exists():
        raise SystemExit(f"JSON file not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise SystemExit(f"Expected JSON array in {path}")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync staffing data and build staffing snapshots")
    parser.add_argument("--staff-file", help="Path to normalized staff JSON array")
    parser.add_argument("--plan-file", help="Path to normalized shift plan JSON array")
    parser.add_argument("--fact-file", help="Path to normalized shift fact JSON array")
    parser.add_argument(
        "--snapshot-date",
        action="append",
        default=[],
        help="Snapshot date in YYYY-MM-DD; may be repeated",
    )
    args = parser.parse_args()

    result = run_staffing_sync(
        staff_payload=_load_json_array(args.staff_file),
        shift_plan_payload=_load_json_array(args.plan_file),
        shift_fact_payload=_load_json_array(args.fact_file),
        snapshot_dates=[_parse_date(item) for item in args.snapshot_date],
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
