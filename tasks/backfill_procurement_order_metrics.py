"""Read-only 1C enrichment and safe local metrics backfill."""

from __future__ import annotations

import argparse
import csv
import json
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.infrastructure.db.engines import build_engine
from app.services.procurement_order_metrics import DEFAULT_METRICS_WINDOW_DAYS
from app.services.procurement_order_metrics_backfill import (
    apply_metrics_backfill,
    build_metrics_backfill_plan,
    rollback_metrics_backfill,
)
from tasks.backfill_procurement_order_product_media import (
    load_existing_committed_apply_result,
    write_json_atomic,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Enrich open procurement assistant lines; 1C remains read-only."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--rollback-manifest", type=Path)
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    parser.add_argument("--window-days", type=int, default=DEFAULT_METRICS_WINDOW_DAYS)
    parser.add_argument("--lead-time-csv", type=Path)
    parser.add_argument(
        "--order-ids-from-json",
        type=Path,
        help="Limit processing to persisted_order_ids from an order-formation JSON result.",
    )
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.window_days <= 0:
        raise SystemExit("--window-days must be positive")
    if args.apply and args.output_json is None:
        raise SystemExit("--output-json is required with --apply")
    if args.apply:
        existing = load_existing_committed_apply_result(
            args.output_json,
            run_id=args.run_id,
        )
        if existing is not None:
            _print_result(existing, compact=args.json)
            return 0
    settings = get_settings()
    app_engine = build_engine(settings.database_url)
    if args.rollback_manifest:
        manifest = json.loads(args.rollback_manifest.read_text(encoding="utf-8"))
        with Session(app_engine) as db:
            try:
                result = rollback_metrics_backfill(db, manifest)
                db.commit()
            except BaseException:
                db.rollback()
                raise
        if args.output_json:
            write_json_atomic(args.output_json, result)
        _print_result(result, compact=args.json)
        return 0
    if not settings.onec_database_url:
        raise SystemExit("ONEC_DATABASE_URL is required")
    lead_time_path = args.lead_time_csv or (
        REPO_ROOT
        / "reports"
        / "assortment_lifecycle"
        / args.as_of.isoformat()
        / "display-supplier-lead-time-history.csv"
    )
    lead_time_rows = _read_csv(lead_time_path) if lead_time_path.exists() else []
    order_ids = (
        _read_persisted_order_ids(args.order_ids_from_json)
        if args.order_ids_from_json is not None
        else None
    )
    onec_engine = build_engine(settings.onec_database_url, pool_pre_ping=True)
    try:
        with Session(app_engine) as db:
            plan = build_metrics_backfill_plan(
                db,
                onec_engine,
                lead_time_rows=lead_time_rows,
                as_of=args.as_of,
                window_days=args.window_days,
                run_id=args.run_id or None,
                order_ids=order_ids,
            )
            if not args.apply:
                db.rollback()
                if args.output_json:
                    write_json_atomic(args.output_json, plan)
                _print_result(plan, compact=args.json)
                return 0
            try:
                result = apply_metrics_backfill(db, plan)
                write_json_atomic(args.output_json, result)
                db.commit()
                result["database_commit"] = True
                write_json_atomic(args.output_json, result)
            except BaseException:
                db.rollback()
                raise
    finally:
        onec_engine.dispose()
    _print_result(result, compact=args.json)
    return 0


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        return [dict(row) for row in csv.DictReader(source)]


def _read_persisted_order_ids(path: Path) -> list[int]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"cannot read --order-ids-from-json: {path}") from exc
    raw_order_ids = payload.get("persisted_order_ids") if isinstance(payload, dict) else None
    if not isinstance(raw_order_ids, list):
        raise SystemExit("--order-ids-from-json must contain persisted_order_ids list")
    try:
        return sorted({int(order_id) for order_id in raw_order_ids})
    except (TypeError, ValueError) as exc:
        raise SystemExit("persisted_order_ids must contain integer order ids") from exc


def _print_result(payload: Mapping[str, Any], *, compact: bool) -> None:
    output = {
        "run_id": payload.get("run_id"),
        "mode": payload.get("mode"),
        "database_commit": payload.get("database_commit", payload.get("mode") == "rollback"),
        "summary": payload.get("summary") or {},
        "safety": payload.get("safety") or {},
    }
    print(json.dumps(output, ensure_ascii=False, indent=None if compact else 2, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
