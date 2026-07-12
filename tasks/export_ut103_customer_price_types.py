from __future__ import annotations

import argparse
import csv
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from app.services.exporters.ut103_customer_price_types import (
    APPROVED_DECISION,
    DEFAULT_SOURCE,
    CustomerPriceTypeUpdateMessage,
    CustomerPriceTypeUpdateRow,
    build_customer_price_type_updates_xml,
    list_customer_price_type_exchange_results,
    one_c_guid_from_counterparty_ref,
    write_customer_price_type_updates_message,
)
from app.services.exporters.ut103_exchange import load_ut103_env_file, resolve_ut103_exchange_root


def main() -> int:
    load_ut103_env_file()
    args = _parse_args()
    message_id = (
        args.message_id or f"customer-price-types-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    )

    if args.list_results:
        exchange_root = _exchange_root_or_exit(args.exchange_root)
        print(
            json.dumps(
                [
                    _result_to_json(result)
                    for result in list_customer_price_type_exchange_results(exchange_root)
                ],
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0

    rows = _rows_from_csv(args.input_csv, message_id)
    message = CustomerPriceTypeUpdateMessage(
        message_id=message_id,
        rows=tuple(rows),
        mode=args.mode,
        approved_by=args.approved_by,
        source=args.source,
    )
    payload = build_customer_price_type_updates_xml(message)
    summary: dict[str, Any] = {
        "message_id": message.message_id,
        "mode": message.mode,
        "rows": len(message.rows),
        "schema": message.schema,
        "validated_only": bool(args.validate_only),
    }
    if args.validate_only:
        if args.json:
            print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        else:
            print(payload.decode("windows-1251"))
        return 0

    output_path = write_customer_price_type_updates_message(
        _exchange_root_or_exit(args.exchange_root), message, overwrite=args.overwrite
    )
    summary["path"] = str(output_path)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    else:
        print(output_path)
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export approved 2.Бронзовый -> Розница customer changes to UT 10.3."
    )
    parser.add_argument("--exchange-root", help="UT103 exchange root, e.g. /mnt/ut103")
    parser.add_argument("--message-id", help="Stable id for this one approved batch")
    parser.add_argument("--mode", choices=("dry_run", "apply"), default="dry_run")
    parser.add_argument("--approved-by", default="", help="Required for an apply package")
    parser.add_argument(
        "--source",
        default=os.environ.get("UT103_CUSTOMER_PRICE_TYPES_SOURCE", DEFAULT_SOURCE),
    )
    parser.add_argument(
        "--input-csv",
        type=Path,
        help="Approved CSV with counterparty_ref/current_price_type/target_price_type fields",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate and print the XML (or JSON summary); do not create a ready file",
    )
    parser.add_argument("--overwrite", action="store_true", help="Overwrite an existing ready file")
    parser.add_argument("--json", action="store_true", help="Print machine-readable summary")
    parser.add_argument(
        "--list-results",
        action="store_true",
        help="Print parsed customer_price_types result files and exit",
    )
    args = parser.parse_args()
    if args.list_results:
        return args
    if args.input_csv is None:
        parser.error("--input-csv is required unless --list-results is used")
    return args


def _rows_from_csv(path: Path, message_id: str) -> list[CustomerPriceTypeUpdateRow]:
    with path.open("r", encoding="utf-8-sig", newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        if reader.fieldnames is None:
            raise SystemExit("CSV must contain a header row")
        return [_row_from_mapping(row, message_id) for row in reader]


def _row_from_mapping(item: dict[str, Any], message_id: str) -> CustomerPriceTypeUpdateRow:
    counterparty_ref = str(_field(item, "counterparty_ref", "CounterpartyRef"))
    return CustomerPriceTypeUpdateRow(
        idempotency_key=str(
            _optional_field(
                item,
                "idempotency_key",
                "IdempotencyKey",
                default=f"customer-price-type:{message_id}:{counterparty_ref}",
            )
        ),
        counterparty_ref=counterparty_ref,
        counterparty_guid=str(
            _optional_field(
                item,
                "counterparty_guid",
                "CounterpartyGuid",
                default=one_c_guid_from_counterparty_ref(counterparty_ref),
            )
        ),
        counterparty_name=str(_field(item, "counterparty_name", "CounterpartyName")),
        expected_current_price_type=str(
            _field(
                item,
                "current_price_type",
                "expected_current_price_type",
                "ExpectedCurrentPriceType",
            )
        ),
        target_price_type=str(_field(item, "target_price_type", "TargetPriceType")),
        decision=str(_optional_field(item, "decision", "Decision", default=APPROVED_DECISION)),
        reason=str(_optional_field(item, "reason", "Reason", default="")),
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


def _exchange_root_or_exit(explicit: str | Path | None) -> str:
    try:
        return resolve_ut103_exchange_root(explicit)
    except ValueError as error:
        raise SystemExit(str(error)) from error


def _result_to_json(result: Any) -> dict[str, Any]:
    return {
        "message_id": result.message_id,
        "status": result.status,
        "loaded": result.loaded,
        "failed": result.failed,
        "errors": result.errors,
        "path": str(result.path) if result.path else None,
        "item_results": [
            {
                "idempotency_key": item.idempotency_key,
                "counterparty_ref": item.counterparty_ref,
                "counterparty_guid": item.counterparty_guid,
                "counterparty_name": item.counterparty_name,
                "result": item.result,
                "message": item.message,
                "contract_guid": item.contract_guid,
                "contract_name": item.contract_name,
                "current_price_type": item.current_price_type,
                "target_price_type": item.target_price_type,
                "found_contracts": item.found_contracts,
            }
            for item in result.item_results
        ],
    }


if __name__ == "__main__":
    raise SystemExit(main())
