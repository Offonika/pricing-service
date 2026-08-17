"""Build one source-backed first-wave signal bundle without persistence."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from app.infrastructure.db import session_scope
from app.infrastructure.db.engines import (
    DatabaseNotConfiguredError,
    build_onec_engine_from_settings,
)
from app.services.assortment_lifecycle_signal_ingestion import (
    AssortmentLifecycleSignalIngestionError,
    display_family_registry_snapshot_from_mapping,
    load_active_display_family_registry_snapshot,
)
from app.services.assortment_lifecycle_signal_sources import (
    AssortmentLifecycleSignalSourceError,
    extract_assortment_signal_source_bundle,
    load_document_line_mapping,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SUPPLIER_ORDER_MAPPING_JSON = (
    REPO_ROOT / "config/assortment/display-supplier-order-line-mapping.json"
)
DEFAULT_RECEIPT_MAPPING_JSON = REPO_ROOT / "config/assortment/display-receipt-line-mapping.json"
SOURCE_BUILD_ERROR_SCHEMA = "assortment_signal_source_bundle_build_error.v1"


def _aware_datetime(value: str) -> datetime:
    source = value.strip()
    if source.endswith("Z"):
        source = source[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(source)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("datetime must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("datetime must include a timezone")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read five procurement sources and write an auditable signal source bundle. "
            "The command never persists signals or changes 1C/application state."
        )
    )
    parser.add_argument(
        "--date-from",
        type=_aware_datetime,
        required=True,
        help="Inclusive timezone-aware source window start",
    )
    parser.add_argument(
        "--as-of",
        type=_aware_datetime,
        required=True,
        help="Inclusive timezone-aware source event cutoff",
    )
    parser.add_argument(
        "--family-registry-json",
        type=Path,
        help=(
            "Optional display_family_registry_snapshot.v1 fixture; otherwise the active "
            "application registry is read through a read-only session"
        ),
    )
    parser.add_argument(
        "--supplier-order-mapping-json",
        type=Path,
        default=DEFAULT_SUPPLIER_ORDER_MAPPING_JSON,
    )
    parser.add_argument(
        "--receipt-mapping-json",
        type=Path,
        default=DEFAULT_RECEIPT_MAPPING_JSON,
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        required=True,
        help="Destination for assortment_signal_source_bundle.v1",
    )
    return parser


def _read_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise AssortmentLifecycleSignalSourceError(f"json_root_must_be_object:{path.name}")
    return payload


def _blocked_payload(*, error: str, error_type: str) -> dict[str, Any]:
    return {
        "schema": SOURCE_BUILD_ERROR_SCHEMA,
        "status": "blocked",
        "dry_run": True,
        "production_authorized": False,
        "persistence_performed": False,
        "external_writes": False,
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
    protected_inputs = {
        args.supplier_order_mapping_json.resolve(),
        args.receipt_mapping_json.resolve(),
    }
    if args.family_registry_json is not None:
        protected_inputs.add(args.family_registry_json.resolve())
    if args.output_json.resolve() in protected_inputs:
        result = _blocked_payload(
            error="output_json_must_not_overwrite_an_input_artifact",
            error_type="OutputArtifactPathConflict",
        )
        return 2, result

    engine = None
    try:
        if args.family_registry_json is not None:
            registry_snapshot = display_family_registry_snapshot_from_mapping(
                _read_json_object(args.family_registry_json),
                source=f"json_fixture:{args.family_registry_json.name}",
            )
        else:
            with session_scope(read_only=True) as session:
                registry_snapshot = load_active_display_family_registry_snapshot(session)
        supplier_mapping = load_document_line_mapping(
            args.supplier_order_mapping_json,
            error_code="supplier_order_mapping_unresolved",
        )
        receipt_mapping = load_document_line_mapping(
            args.receipt_mapping_json,
            error_code="supplier_receipt_mapping_unresolved",
        )
        engine = build_onec_engine_from_settings()
        result = extract_assortment_signal_source_bundle(
            engine,
            registry_snapshot,
            date_from=args.date_from,
            as_of=args.as_of,
            supplier_order_mapping=supplier_mapping,
            supplier_receipt_mapping=receipt_mapping,
        )
        exit_code = 2 if result["data_quality"]["status"] == "blocked" else 0
    except (
        AssortmentLifecycleSignalIngestionError,
        AssortmentLifecycleSignalSourceError,
    ) as exc:
        result = _blocked_payload(error=str(exc), error_type=type(exc).__name__)
        exit_code = 2
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        result = _blocked_payload(
            error="input_artifact_read_failed",
            error_type=type(exc).__name__,
        )
        exit_code = 2
    except DatabaseNotConfiguredError as exc:
        result = _blocked_payload(
            error="onec_database_not_configured",
            error_type=type(exc).__name__,
        )
        exit_code = 2
    except SQLAlchemyError as exc:
        result = _blocked_payload(
            error="read_only_source_query_failed",
            error_type=type(exc).__name__,
        )
        exit_code = 2
    finally:
        if engine is not None:
            engine.dispose()
    _write_artifact(args.output_json, result)
    return exit_code, result


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    exit_code, payload = run(args)
    print(
        json.dumps(
            {
                "schema": payload.get("schema"),
                "status": payload.get("data_quality", {}).get("status", payload.get("status")),
                "output_json": str(args.output_json),
                "item_count": len(payload.get("items") or ()),
                "production_authorized": False,
                "persistence_performed": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
