"""Capture a fail-closed, append-only 1C pipeline observation.

Only local evidence artifacts are written.  The command has no application DB,
order, status, recommendation, Bitrix24, Telegram, release or production-cron
write mode.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from app.infrastructure.db.engines import build_onec_engine_from_settings
from app.services.margin_flow_pipeline_observer import (
    default_observation_slot,
    file_sha256,
    load_observer_config,
    load_scope_codes,
    observer_lock,
    read_source_snapshot,
    reuse_existing_observation,
    validate_observer_bundle,
    write_observation,
)

DEFAULT_CONFIG = Path("config/assortment/margin-flow-pipeline-observer.json")
DEFAULT_SCOPE_CSV = Path(
    "reports/assortment_lifecycle/experiments/"
    "2026-08-16-margin-flow-reorder-point-readonly-v1/run/current-snapshot-diff.csv"
)
DEFAULT_OUTPUT_ROOT = Path(
    "reports/assortment_lifecycle/experiments/"
    "2026-08-17-margin-flow-pipeline-forward-observer-v1"
)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Observe the frozen Margin Flow cohort's open 1C pipeline into a local "
            "append-only evidence chain."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    capture = subparsers.add_parser("capture")
    capture.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    capture.add_argument("--scope-csv", type=Path, default=DEFAULT_SCOPE_CSV)
    capture.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    capture.add_argument("--observation-slot")
    capture.add_argument("--json", action="store_true")

    for command in ("validate", "status"):
        child = subparsers.add_parser(command)
        child.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
        child.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def _capture(args: argparse.Namespace) -> dict[str, object]:
    config = load_observer_config(args.config)
    scope_codes = load_scope_codes(args.scope_csv, config)
    observation_slot = args.observation_slot or default_observation_slot(
        timezone_name=config.timezone_name
    )
    sanitized_command = [
        "python",
        "-m",
        "tasks.observe_margin_flow_pipeline",
        "capture",
        "--config",
        str(args.config),
        "--scope-csv",
        str(args.scope_csv),
        "--output-root",
        str(args.output_root),
        "--observation-slot",
        observation_slot,
    ]
    with observer_lock(args.output_root):
        existing = reuse_existing_observation(
            output_root=args.output_root,
            observation_slot=observation_slot,
            config=config,
            scope_sha256=file_sha256(args.scope_csv),
        )
        if existing is not None:
            result = existing
        else:
            engine = build_onec_engine_from_settings()
            try:
                raw_lots, permissions, started_at, completed_at = read_source_snapshot(
                    engine,
                    codes=scope_codes,
                )
            finally:
                engine.dispose()
            result = write_observation(
                output_root=args.output_root,
                observation_slot=observation_slot,
                config=config,
                scope_path=args.scope_csv,
                scope_codes=scope_codes,
                raw_lots=raw_lots,
                permission_evidence=permissions,
                source_read_started_at=started_at,
                source_read_completed_at=completed_at,
                command=sanitized_command,
            )
        validation = validate_observer_bundle(args.output_root)
    return {
        "status": "reused" if result.reused else "captured",
        "observation_slot": result.observation_slot,
        "observation_dir": str(result.observation_dir),
        "manifest_sha256": result.manifest_sha256,
        "lot_count": result.lot_count,
        "scope_code_count": result.scope_code_count,
        "source": "onec_read_only",
        "local_artifact_write": True,
        "external_writes": False,
        "application_database_writes": False,
        "recommended_order_qty_calculated": False,
        "production_action": "none",
        "observer_status": validation["status"],
        "remaining_consecutive_days": validation["remaining_consecutive_days"],
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    payload = (
        _capture(args) if args.command == "capture" else validate_observer_bundle(args.output_root)
    )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"margin-flow pipeline observer: {payload['status']} " f"({args.output_root})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
