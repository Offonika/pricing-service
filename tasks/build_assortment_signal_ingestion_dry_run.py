"""Build a reconciled first-wave signal-ingestion artifact without persistence."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from app.infrastructure.db import session_scope
from app.services.assortment_lifecycle_signal_ingestion import (
    SIGNAL_INGESTION_ARTIFACT_SCHEMA,
    AssortmentLifecycleSignalIngestionError,
    build_assortment_signal_ingestion_dry_run,
    display_family_registry_snapshot_from_mapping,
    load_active_display_family_registry_snapshot,
)
from app.services.assortment_lifecycle_signal_sources import (
    source_bundle_embedded_registry,
)


def _as_of(value: str) -> datetime:
    source = value.strip()
    if source.endswith("Z"):
        source = source[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(source)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--as-of must be an ISO datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("--as-of must include a timezone")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare and reconcile first-wave assortment signals. "
            "The command only writes the requested JSON artifact."
        )
    )
    parser.add_argument(
        "--input-json",
        type=Path,
        required=True,
        help="Normalized assortment_signal_source_bundle.v1 JSON",
    )
    parser.add_argument(
        "--family-registry-json",
        type=Path,
        help=(
            "Optional display_family_registry_snapshot.v1 fixture; without it the active "
            "application registry is read via a read-only session"
        ),
    )
    parser.add_argument(
        "--as-of",
        type=_as_of,
        help="Optional timezone-aware cutoff overriding input JSON as_of",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        required=True,
        help="Destination for the dry-run reconciliation artifact",
    )
    return parser


def _read_json(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    payload = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(payload, dict):
        raise AssortmentLifecycleSignalIngestionError(f"json_root_must_be_object:{path.name}")
    return payload, hashlib.sha256(raw).hexdigest()


def _blocked_payload(*, error: str, error_type: str) -> dict[str, Any]:
    return {
        "schema": SIGNAL_INGESTION_ARTIFACT_SCHEMA,
        "status": "blocked",
        "dry_run": True,
        "production_authorized": False,
        "persistence_performed": False,
        "external_writes": False,
        "signal_release_allowed": False,
        "error": error,
        "error_type": error_type,
    }


def _write_artifact(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(content)
        temporary_path.replace(path)
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def run(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    protected_inputs = {args.input_json.resolve()}
    if args.family_registry_json is not None:
        protected_inputs.add(args.family_registry_json.resolve())
    if args.output_json.resolve() in protected_inputs:
        return 2, _blocked_payload(
            error="output_json_must_not_overwrite_an_input_artifact",
            error_type="OutputArtifactPathConflict",
        )
    try:
        source_bundle, source_sha256 = _read_json(args.input_json)
        registry_sha256: str | None = None
        if args.family_registry_json is not None:
            registry_payload, registry_sha256 = _read_json(args.family_registry_json)
            registry_snapshot = display_family_registry_snapshot_from_mapping(
                registry_payload,
                source=f"json_fixture:{args.family_registry_json.name}",
            )
        else:
            embedded_registry = source_bundle_embedded_registry(source_bundle)
            if embedded_registry is not None:
                registry_snapshot = display_family_registry_snapshot_from_mapping(
                    embedded_registry,
                    source=f"embedded_source_bundle:{args.input_json.name}",
                )
            else:
                with session_scope(read_only=True) as session:
                    registry_snapshot = load_active_display_family_registry_snapshot(session)
        result = build_assortment_signal_ingestion_dry_run(
            source_bundle,
            registry_snapshot,
            as_of=args.as_of,
        )
        result["source_artifact"] = {
            "path": str(args.input_json),
            "sha256": source_sha256,
        }
        if args.family_registry_json is not None:
            result["family_registry_artifact"] = {
                "path": str(args.family_registry_json),
                "sha256": registry_sha256,
            }
        exit_code = 2 if result["status"].startswith("blocked") else 0
    except AssortmentLifecycleSignalIngestionError as exc:
        result = _blocked_payload(error=str(exc), error_type=type(exc).__name__)
        exit_code = 2
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        result = _blocked_payload(error="input_artifact_read_failed", error_type=type(exc).__name__)
        exit_code = 2
    except SQLAlchemyError as exc:
        result = _blocked_payload(
            error="active_family_registry_read_failed",
            error_type=type(exc).__name__,
        )
        exit_code = 2
    _write_artifact(args.output_json, result)
    return exit_code, result


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    exit_code, payload = run(args)
    print(
        json.dumps(
            {
                "status": payload["status"],
                "output_json": str(args.output_json),
                "prepared_signal_count": len(payload.get("prepared_signals") or ()),
                "quarantine_count": len(payload.get("quarantine") or ()),
                "conflict_count": len(payload.get("conflicts") or ()),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
