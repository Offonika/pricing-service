from __future__ import annotations

import argparse
import csv
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from app.services.exporters.ut103_exchange import load_ut103_env_file, resolve_ut103_exchange_root
from app.services.exporters.ut103_nomenclature_properties import (
    DEFAULT_SOURCE,
    NomenclaturePropertyUpdateMessage,
    NomenclaturePropertyUpdateRow,
    build_nomenclature_property_updates_xml,
    list_property_update_exchange_results,
    write_nomenclature_property_updates_message,
)


def main() -> int:
    load_ut103_env_file()
    args = _parse_args()
    rows = _load_rows(args)
    message_id = (
        args.message_id or f"nomenclature-properties-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    )
    message = NomenclaturePropertyUpdateMessage(
        message_id=message_id,
        rows=tuple(rows),
        mode=args.mode,
        approved_by=args.approved_by,
        source=args.source,
    )

    if args.dry_run:
        print(build_nomenclature_property_updates_xml(message).decode("windows-1251"))
        return 0

    try:
        exchange_root = resolve_ut103_exchange_root(args.exchange_root)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    output_path = write_nomenclature_property_updates_message(
        exchange_root,
        message,
        overwrite=args.overwrite,
    )
    if args.json:
        print(
            json.dumps(
                {
                    "message_id": message.message_id,
                    "mode": message.mode,
                    "rows": len(message.rows),
                    "path": str(output_path),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    else:
        print(output_path)
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export nomenclature_property_updates.v1 XML into a UT 10.3 exchange folder."
    )
    parser.add_argument("--exchange-root", help="UT103 exchange root, e.g. /mnt/ut103")
    parser.add_argument("--message-id", help="Stable idempotency key for this update package")
    parser.add_argument("--mode", choices=("dry_run", "apply"), default="dry_run")
    parser.add_argument(
        "--approved-by", default="", help="Required for apply unless every row has ApprovedBy"
    )
    parser.add_argument(
        "--source",
        default=os.environ.get("UT103_NOMENCLATURE_PROPERTIES_SOURCE", DEFAULT_SOURCE),
    )
    parser.add_argument("--input-csv", type=Path, help="CSV with property update row headers")
    parser.add_argument("--input-json", type=Path, help="JSON list or {items:[...]} with rows")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing ready file")
    parser.add_argument("--dry-run", action="store_true", help="Print XML without writing a file")
    parser.add_argument("--json", action="store_true", help="Print machine-readable summary")
    parser.add_argument(
        "--list-results",
        action="store_true",
        help="Print parsed from_1c/new result files and exit",
    )
    args = parser.parse_args()

    if args.list_results:
        try:
            exchange_root = resolve_ut103_exchange_root(args.exchange_root)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        results = [
            {
                "message_id": result.message_id,
                "status": result.status,
                "loaded": result.loaded,
                "failed": result.failed,
                "errors": result.errors,
                "item_results": [
                    {
                        "idempotency_key": item.idempotency_key,
                        "nomenclature_code": item.nomenclature_code,
                        "property_name": item.property_name,
                        "result": item.result,
                        "message": item.message,
                        "current_value": item.current_value,
                        "new_value": item.new_value,
                    }
                    for item in result.item_results
                ],
                "path": str(result.path) if result.path else None,
            }
            for result in list_property_update_exchange_results(exchange_root)
        ]
        print(json.dumps(results, ensure_ascii=False, sort_keys=True))
        raise SystemExit(0)

    sources_count = sum(bool(value) for value in (args.input_csv, args.input_json))
    if sources_count != 1:
        raise SystemExit("Pass exactly one row source: --input-csv or --input-json")
    return args


def _load_rows(args: argparse.Namespace) -> list[NomenclaturePropertyUpdateRow]:
    if args.input_csv:
        return _rows_from_csv(args.input_csv)
    if args.input_json:
        return _rows_from_json(args.input_json)
    raise SystemExit("No property update rows provided")


def _rows_from_csv(path: Path) -> list[NomenclaturePropertyUpdateRow]:
    with path.open("r", encoding="utf-8-sig", newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        return [_row_from_mapping(row) for row in reader]


def _rows_from_json(path: Path) -> list[NomenclaturePropertyUpdateRow]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    items = payload.get("items") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise SystemExit("JSON must be a list or an object with an items list")
    return [_row_from_mapping(item) for item in items]


def _row_from_mapping(item: dict[str, Any]) -> NomenclaturePropertyUpdateRow:
    return NomenclaturePropertyUpdateRow(
        idempotency_key=str(_field(item, "idempotency_key", "IdempotencyKey")),
        nomenclature_code=str(_field(item, "nomenclature_code", "NomenclatureCode")),
        property_name=str(_field(item, "property_name", "PropertyName")),
        value_type=str(_field(item, "value_type", "ValueType")),
        new_value=_optional_field(item, "new_value", "NewValue"),
        new_value_name=str(_optional_field(item, "new_value_name", "NewValueName", default="")),
        new_value_tag=str(_optional_field(item, "new_value_tag", "NewValueTag", default="")),
        expected_current_value_name=str(
            _optional_field(
                item, "expected_current_value_name", "ExpectedCurrentValueName", default=""
            )
        ),
        expected_current_value_tag=str(
            _optional_field(
                item, "expected_current_value_tag", "ExpectedCurrentValueTag", default=""
            )
        ),
        reason=str(_optional_field(item, "reason", "Reason", default="")),
        approved_by=str(_optional_field(item, "approved_by", "ApprovedBy", default="")),
    )


def _field(item: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in item and item[name] not in (None, ""):
            return item[name]
    raise SystemExit(f"Missing required field; expected one of: {', '.join(names)}")


def _optional_field(item: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in item and item[name] not in (None, ""):
            return item[name]
    return default


if __name__ == "__main__":
    raise SystemExit(main())
