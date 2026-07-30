from __future__ import annotations

import argparse
import json
import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.infrastructure.db.engines import build_engine
from app.services.assortment_lifecycle_facts import (
    DEFAULT_HISTORY_MONTHS,
    RECEIPT_MAPPING_UNRESOLVED,
    SUPPLIER_ORDER_MAPPING_UNRESOLVED,
    DocumentLineMapping,
    build_assortment_lifecycle_fact_records,
    default_history_start,
    enrich_nomenclature_rows_with_product_snapshot,
    fetch_onec_lifecycle_source_rows,
    normalize_manager_signals,
    normalize_manual_overrides,
    validate_warehouse_policy,
)
from app.services.exporters.ut103_exchange import load_ut103_env_file
from app.services.onec_stock_availability import (
    DEFAULT_HISTORY_DAYS as DEFAULT_AVAILABILITY_HISTORY_DAYS,
)
from app.services.onec_stock_availability import (
    attach_effective_availability_shadow_to_facts,
)

DEFAULT_OUTPUT_PATH = Path("build/assortment/assortment-lifecycle-facts.json")


def main() -> int:
    load_ut103_env_file()
    args = _parse_args()
    settings = get_settings()
    warehouse_policy = validate_warehouse_policy(_load_json_object(args.warehouse_policy_json))
    manual_overrides = normalize_manual_overrides(_load_optional_json(args.manual_overrides_json))
    manager_signals = normalize_manager_signals(_load_optional_json(args.manager_signals_json))
    history_start = default_history_start(args.today, history_months=args.history_months)

    try:
        if args.input_json:
            raw_payload = _load_json_object(args.input_json)
            nomenclature_rows, supplier_order_rows, receipt_rows = _source_rows_from_payload(
                raw_payload
            )
        else:
            onec_database_url = (
                args.onec_database_url
                or os.environ.get("ONEC_DATABASE_URL", "")
                or settings.onec_database_url
                or ""
            )
            if not onec_database_url:
                raise SystemExit("ONEC_DATABASE_URL is required unless --input-json is passed")
            supplier_mapping = _load_document_line_mapping(
                args.supplier_order_mapping_json,
                error_code=SUPPLIER_ORDER_MAPPING_UNRESOLVED,
            )
            receipt_mapping = _load_document_line_mapping(
                args.receipt_mapping_json,
                error_code=RECEIPT_MAPPING_UNRESOLVED,
            )
            engine = build_engine(onec_database_url, pool_pre_ping=True)
            try:
                nomenclature_rows, supplier_order_rows, receipt_rows = (
                    fetch_onec_lifecycle_source_rows(
                        engine,
                        folder=args.folder,
                        history_start=history_start,
                        supplier_mapping=supplier_mapping,
                        receipt_mapping=receipt_mapping,
                        limit=args.limit,
                    )
                )
            finally:
                engine.dispose()
            product_engine = build_engine(settings.database_url, pool_pre_ping=True)
            try:
                nomenclature_rows = enrich_nomenclature_rows_with_product_snapshot(
                    product_engine,
                    nomenclature_rows,
                )
            finally:
                product_engine.dispose()
    except ValueError as exc:
        if args.json:
            print(
                json.dumps(
                    {"status": "blocked", "error": str(exc)},
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        raise SystemExit(str(exc)) from exc

    facts, summary = build_assortment_lifecycle_fact_records(
        nomenclature_rows=nomenclature_rows,
        supplier_order_rows=supplier_order_rows,
        receipt_rows=receipt_rows,
        warehouse_policy=warehouse_policy,
        manual_overrides=manual_overrides,
        manager_signals=manager_signals,
        history_start=history_start,
    )
    if not args.input_json:
        product_engine = build_engine(settings.database_url, pool_pre_ping=True)
        try:
            facts = attach_effective_availability_shadow_to_facts(
                product_engine,
                facts,
                date_to=(args.today or date.today()) - timedelta(days=1),
                history_days=DEFAULT_AVAILABILITY_HISTORY_DAYS,
            )
        finally:
            product_engine.dispose()
    payload = {
        "meta": {
            "schema": "assortment_lifecycle_facts.v1",
            "folder": args.folder,
            "history_months": args.history_months,
            "history_start": history_start.isoformat(),
            "source": "input_json" if args.input_json else "onec_read_only",
        },
        "items": facts,
    }
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    if args.json:
        print(
            json.dumps(
                {
                    "status": "ready",
                    "folder": args.folder,
                    "items": len(facts),
                    "summary": summary,
                    "output_json": str(args.output_json) if args.output_json else None,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    else:
        print(f"items={len(facts)}")
        if args.output_json:
            print(args.output_json)
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build read-only assortment_lifecycle_facts.v1 for the display pilot. "
            "The output is accepted by tasks.build_assortment_lifecycle_updates."
        )
    )
    parser.add_argument("--folder", default="дисплеи", help="Pilot folder filter")
    parser.add_argument("--history-months", type=int, default=_default_history_months())
    parser.add_argument("--today", type=_parse_date, default=None)
    parser.add_argument("--limit", type=int, default=_default_limit())
    parser.add_argument("--onec-database-url", default="")
    parser.add_argument(
        "--input-json",
        type=Path,
        help="Offline source rows fixture: {nomenclature_rows, supplier_order_rows, receipt_rows}",
    )
    parser.add_argument("--warehouse-policy-json", type=Path, required=True)
    parser.add_argument("--supplier-order-mapping-json", type=Path)
    parser.add_argument("--receipt-mapping-json", type=Path)
    parser.add_argument("--manual-overrides-json", type=Path)
    parser.add_argument("--manager-signals-json", type=Path)
    parser.add_argument("--output-json", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--json", action="store_true", help="Print machine-readable summary")
    args = parser.parse_args()
    if args.history_months <= 0:
        raise SystemExit("--history-months must be positive")
    if args.limit <= 0:
        raise SystemExit("--limit must be positive")
    return args


def _default_history_months() -> int:
    raw_value = os.getenv("ASSORTMENT_LIFECYCLE_HISTORY_MONTHS")
    if raw_value is None or raw_value == "":
        return DEFAULT_HISTORY_MONTHS
    try:
        return int(raw_value)
    except ValueError as exc:
        raise SystemExit("ASSORTMENT_LIFECYCLE_HISTORY_MONTHS must be an integer") from exc


def _default_limit() -> int:
    raw_value = os.getenv("ASSORTMENT_LIFECYCLE_LIMIT")
    if raw_value is None or raw_value == "":
        return 3000
    try:
        return int(raw_value)
    except ValueError as exc:
        raise SystemExit("ASSORTMENT_LIFECYCLE_LIMIT must be an integer") from exc


def _load_document_line_mapping(path: Path | None, *, error_code: str) -> DocumentLineMapping:
    if path is None:
        raise ValueError(f"{error_code}: mapping_json_required")
    return DocumentLineMapping.from_mapping(_load_json_object(path))


def _source_rows_from_payload(
    payload: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    return (
        _list_field(payload, "nomenclature_rows"),
        _list_field(payload, "supplier_order_rows"),
        _list_field(payload, "receipt_rows"),
    )


def _list_field(payload: dict[str, Any], field_name: str) -> list[dict[str, Any]]:
    raw = payload.get(field_name)
    if not isinstance(raw, list):
        raise SystemExit(f"{field_name} must be a list")
    if not all(isinstance(item, dict) for item in raw):
        raise SystemExit(f"{field_name} items must be objects")
    return raw


def _load_optional_json(path: Path | None) -> Any:
    if path is None:
        return None
    return _load_json(path)


def _load_json_object(path: Path) -> dict[str, Any]:
    payload = _load_json(path)
    if not isinstance(payload, dict):
        raise SystemExit(f"{path} must contain a JSON object")
    return payload


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"date must be YYYY-MM-DD, got: {value}") from exc


if __name__ == "__main__":
    raise SystemExit(main())
