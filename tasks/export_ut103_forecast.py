from __future__ import annotations

import argparse
import csv
import json
import os
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.services.exporters.ut103_exchange import load_ut103_env_file, resolve_ut103_exchange_root
from app.services.exporters.ut103_forecast import (
    DEFAULT_SOURCE,
    ForecastSalesMessage,
    ForecastSalesRow,
    build_forecast_sales_xml,
    list_exchange_results,
    write_forecast_sales_message,
)


def main() -> int:
    load_ut103_env_file()
    args = _parse_args()
    rows = _load_rows(args)
    message_id = args.message_id or f"forecast-sales-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    message = ForecastSalesMessage(
        message_id=message_id,
        rows=tuple(rows),
        source=args.source,
    )

    if args.dry_run:
        print(build_forecast_sales_xml(message).decode("windows-1251"))
        return 0

    try:
        exchange_root = resolve_ut103_exchange_root(args.exchange_root)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    output_path = write_forecast_sales_message(exchange_root, message, overwrite=args.overwrite)
    if args.json:
        print(
            json.dumps(
                {
                    "message_id": message.message_id,
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
        description="Export forecast_sales.v1 XML into a UT 10.3 exchange folder."
    )
    parser.add_argument("--exchange-root", help="UT103 exchange root, e.g. /mnt/ut103")
    parser.add_argument("--message-id", help="Stable idempotency key for this forecast package")
    parser.add_argument("--source", default=os.environ.get("UT103_FORECAST_SOURCE", DEFAULT_SOURCE))
    parser.add_argument(
        "--row",
        action="append",
        default=[],
        metavar="NOMENCLATURE,WAREHOUSE,YYYY-MM,QTY,AMOUNT",
        help="Forecast row. Can be repeated.",
    )
    parser.add_argument("--input-csv", type=Path, help="CSV with forecast row headers")
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
                "path": str(result.path) if result.path else None,
            }
            for result in list_exchange_results(exchange_root)
        ]
        print(json.dumps(results, ensure_ascii=False, sort_keys=True))
        raise SystemExit(0)

    sources_count = sum(bool(value) for value in (args.row, args.input_csv, args.input_json))
    if sources_count != 1:
        raise SystemExit("Pass exactly one row source: --row, --input-csv or --input-json")
    return args


def _load_rows(args: argparse.Namespace) -> list[ForecastSalesRow]:
    if args.row:
        return [_row_from_csv_line(line) for line in args.row]
    if args.input_csv:
        return _rows_from_csv(args.input_csv)
    if args.input_json:
        return _rows_from_json(args.input_json)
    raise SystemExit("No forecast rows provided")


def _row_from_csv_line(line: str) -> ForecastSalesRow:
    values = next(csv.reader([line]))
    if len(values) != 5:
        raise SystemExit("--row must contain exactly 5 comma-separated values")
    return ForecastSalesRow(
        nomenclature_code=values[0].strip(),
        warehouse_code=values[1].strip(),
        period=values[2].strip(),
        forecast_qty=Decimal(values[3].strip().replace(",", ".")),
        forecast_amount=Decimal(values[4].strip().replace(",", ".")),
    )


def _rows_from_csv(path: Path) -> list[ForecastSalesRow]:
    with path.open("r", encoding="utf-8-sig", newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        return [_row_from_mapping(row) for row in reader]


def _rows_from_json(path: Path) -> list[ForecastSalesRow]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    items = payload.get("items") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise SystemExit("JSON must be a list or an object with an items list")
    return [_row_from_mapping(item) for item in items]


def _row_from_mapping(item: dict[str, Any]) -> ForecastSalesRow:
    return ForecastSalesRow(
        nomenclature_code=str(_field(item, "nomenclature_code", "NomenclatureCode")),
        warehouse_code=str(_field(item, "warehouse_code", "WarehouseCode")),
        period=str(_field(item, "period", "Period")),
        forecast_qty=Decimal(str(_field(item, "forecast_qty", "ForecastQty")).replace(",", ".")),
        forecast_amount=Decimal(
            str(_field(item, "forecast_amount", "ForecastAmount", default="0")).replace(",", ".")
        ),
    )


def _field(item: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in item and item[name] not in (None, ""):
            return item[name]
    if default is not None:
        return default
    raise SystemExit(f"Missing required field; expected one of: {', '.join(names)}")


if __name__ == "__main__":
    raise SystemExit(main())
