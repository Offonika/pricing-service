"""Enrich open procurement assistant lines with exact public catalog media."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.infrastructure.db.engines import build_engine
from app.services.master_mobile_catalog import MasterMobileCatalogResolver
from app.services.procurement_order_product_media import (
    apply_product_media_backfill,
    build_product_media_backfill_plan,
    rollback_product_media_backfill,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Resolve exact product articles at master-mobile.ru. Default mode is read-only dry-run."
        )
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Apply exact matches to open lines.")
    mode.add_argument(
        "--rollback-manifest",
        type=Path,
        help="Restore payloads from a previously applied manifest.",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        help="Write dry-run report or mandatory rollback manifest for --apply.",
    )
    parser.add_argument("--run-id", default="")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.apply and args.output_json is None:
        raise SystemExit("--output-json is required with --apply")
    if args.apply:
        existing_result = load_existing_committed_apply_result(
            args.output_json,
            run_id=args.run_id,
        )
        if existing_result is not None:
            _print_result(existing_result, compact=args.json)
            return 0
    settings = get_settings()
    engine = build_engine(settings.database_url)
    if args.rollback_manifest:
        manifest = json.loads(args.rollback_manifest.read_text(encoding="utf-8"))
        with Session(engine) as db:
            try:
                result = rollback_product_media_backfill(db, manifest)
                db.commit()
            except BaseException:
                db.rollback()
                raise
        if args.output_json:
            write_json_atomic(args.output_json, result)
        _print_result(result, compact=args.json)
        return 0

    resolver = MasterMobileCatalogResolver(
        base_url=settings.master_mobile_catalog_base_url,
        timeout_seconds=settings.master_mobile_catalog_timeout_seconds,
        max_attempts=settings.master_mobile_catalog_max_attempts,
        max_workers=settings.master_mobile_catalog_max_workers,
    )
    with Session(engine) as db:
        plan = build_product_media_backfill_plan(
            db,
            resolver,
            run_id=args.run_id or None,
        )
        if not args.apply:
            db.rollback()
            if args.output_json:
                write_json_atomic(args.output_json, plan)
            _print_result(plan, compact=args.json)
            return 0
        try:
            result = apply_product_media_backfill(db, plan)
            write_json_atomic(args.output_json, result)
            db.commit()
            result["database_commit"] = True
            write_json_atomic(args.output_json, result)
        except BaseException:
            db.rollback()
            raise
    _print_result(result, compact=args.json)
    return 0


def write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=False, indent=2)
            output.write("\n")
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def load_existing_committed_apply_result(
    path: Path,
    *,
    run_id: str,
) -> dict[str, Any] | None:
    path = path.expanduser().resolve()
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"existing --output-json cannot be read safely: {path}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"existing --output-json is not a manifest object: {path}")
    if payload.get("mode") != "apply" or payload.get("database_commit") is not True:
        raise SystemExit(
            "existing --output-json is not a committed apply manifest; "
            "inspect it and use a new path"
        )
    existing_run_id = str(payload.get("run_id") or "").strip()
    if run_id and existing_run_id != run_id:
        raise SystemExit(
            "existing committed apply manifest belongs to another run-id; use a new path"
        )
    return payload


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
