#!/usr/bin/env python3
"""Fail closed when a release would regress task-43 receivables behavior."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

sys.dont_write_bytecode = True

REQUIRED_UI_TEXT = "Долгообразующая накладная"
FORBIDDEN_UI_TEXT = "Подразделение долга"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-dir", default=".")
    parser.add_argument("--snapshot-date", default=date.today().isoformat())
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def validate_release(release_dir: Path, *, snapshot_date: date) -> dict[str, object]:
    from app.infrastructure.db import session_scope
    from app.services.counterparty_folder_recommendations import (
        evaluate_open_debt_source_freshness,
    )

    checks: dict[str, dict[str, object]] = {}
    component_path = release_dir / "ui" / "src" / "components" / "ReceivablesWorkplace.tsx"
    component_text = component_path.read_text(encoding="utf-8") if component_path.exists() else ""
    checks["ui_source"] = {
        "ok": REQUIRED_UI_TEXT in component_text and FORBIDDEN_UI_TEXT not in component_text,
        "path": str(component_path),
    }

    index_path = release_dir / "ui" / "dist" / "index.html"
    index_text = index_path.read_text(encoding="utf-8") if index_path.exists() else ""
    asset_match = re.search(r'src="(?:\./|/)?(assets/[^\"]+\.js)"', index_text)
    asset_path = release_dir / "ui" / "dist" / asset_match.group(1) if asset_match else None
    asset_text = (
        asset_path.read_text(encoding="utf-8") if asset_path and asset_path.exists() else ""
    )
    checks["ui_bundle"] = {
        "ok": REQUIRED_UI_TEXT in asset_text and FORBIDDEN_UI_TEXT not in asset_text,
        "path": str(asset_path or ""),
    }

    with session_scope(read_only=True) as session:
        freshness = evaluate_open_debt_source_freshness(
            session,
            snapshot_date=snapshot_date,
        )
    checks["open_debt_source"] = {
        "ok": freshness.source_status == "cache_ready",
        "source_status": freshness.source_status,
        "source_max_document_date": (
            freshness.source_max_document_date.isoformat()
            if freshness.source_max_document_date
            else None
        ),
        "source_lag_days": freshness.source_lag_days,
    }
    return {
        "ok": all(bool(check.get("ok")) for check in checks.values()),
        "snapshot_date": snapshot_date.isoformat(),
        "checks": checks,
    }


def main() -> int:
    args = _parse_args()
    report = validate_release(
        Path(args.release_dir).resolve(),
        snapshot_date=date.fromisoformat(args.snapshot_date),
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        for name, check in report["checks"].items():
            print(f"{name}: {'OK' if check['ok'] else 'FAIL'}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
