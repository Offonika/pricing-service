from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from app.services.exporters.ut103_exchange import load_ut103_env_file, resolve_ut103_exchange_root
from app.services.exporters.ut103_procurement_orders import (
    DEFAULT_SOURCE,
    OneCReference,
    ProcurementSupplierOrder,
    ProcurementSupplierOrderLine,
    ProcurementSupplierOrderMessage,
    build_procurement_supplier_orders_xml,
    list_procurement_supplier_order_exchange_results,
    write_procurement_supplier_orders_message,
)


def main() -> int:
    load_ut103_env_file()
    args = _parse_args()
    orders = _load_orders(args.input_json)
    message_id = (
        args.message_id or f"procurement-supplier-orders-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    )
    message = ProcurementSupplierOrderMessage(
        message_id=message_id,
        orders=tuple(orders),
        mode=args.mode,
        approved_by=args.approved_by,
        source=args.source,
    )

    if args.dry_run:
        print(build_procurement_supplier_orders_xml(message).decode("windows-1251"))
        return 0

    try:
        exchange_root = resolve_ut103_exchange_root(args.exchange_root)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    output_path = write_procurement_supplier_orders_message(
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
                    "orders": len(message.orders),
                    "lines": sum(len(order.lines) for order in message.orders),
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
        description=(
            "Export procurement_onec_file_exchange.v1 XML draft supplier orders "
            "into a UT 10.3 exchange folder."
        )
    )
    parser.add_argument("--exchange-root", help="UT103 exchange root, e.g. /mnt/ut103")
    parser.add_argument("--message-id", help="Stable idempotency key for this order package")
    parser.add_argument("--mode", choices=("dry_run", "apply"), default="dry_run")
    parser.add_argument(
        "--approved-by", default="", help="Required for apply unless every order has ApprovedBy"
    )
    parser.add_argument(
        "--source",
        default=os.environ.get("UT103_PROCUREMENT_ORDERS_SOURCE", DEFAULT_SOURCE),
    )
    parser.add_argument("--input-json", type=Path, help="JSON order, list, or {orders:[...]}")
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
                        "result": item.result,
                        "message": item.message,
                        "onec_document_ref": item.onec_document_ref,
                        "onec_document_number": item.onec_document_number,
                        "onec_document_date": item.onec_document_date,
                    }
                    for item in result.item_results
                ],
                "path": str(result.path) if result.path else None,
            }
            for result in list_procurement_supplier_order_exchange_results(exchange_root)
        ]
        print(json.dumps(results, ensure_ascii=False, sort_keys=True))
        raise SystemExit(0)

    if not args.input_json:
        raise SystemExit("Pass --input-json")
    return args


def _load_orders(path: Path) -> list[ProcurementSupplierOrder]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(payload, dict):
        raw_orders = payload.get("orders") or payload.get("supplier_orders")
        if raw_orders is None:
            raw_orders = [payload]
    else:
        raw_orders = payload
    if not isinstance(raw_orders, list):
        raise SystemExit("JSON must be an order object, a list, or an object with orders list")
    return [_order_from_mapping(item) for item in raw_orders]


def _order_from_mapping(item: dict[str, Any]) -> ProcurementSupplierOrder:
    if not isinstance(item, dict):
        raise SystemExit("Every order must be an object")
    lines = _optional_field(item, "lines", "Lines", default=None)
    if not isinstance(lines, list):
        raise SystemExit("Every order must contain a lines list")
    return ProcurementSupplierOrder(
        idempotency_key=str(_field(item, "idempotency_key", "IdempotencyKey")),
        order_date=str(_field(item, "order_date", "OrderDate")),
        procurement_contour=str(_field(item, "procurement_contour", "ProcurementContour")),
        supplier=_reference_from_mapping(item, "supplier"),
        contract=_reference_from_mapping(item, "contract"),
        warehouse=_reference_from_mapping(item, "warehouse"),
        currency=str(_field(item, "currency", "Currency")),
        bitrix_item_url=str(_field(item, "bitrix_item_url", "BitrixItemUrl")),
        confirmation_id=str(_field(item, "confirmation_id", "ConfirmationId")),
        calculation_id=str(_field(item, "calculation_id", "CalculationId")),
        lines=tuple(_line_from_mapping(line, index=index) for index, line in enumerate(lines, 1)),
        draft_only=_bool(_optional_field(item, "draft_only", "DraftOnly", default=True)),
        approved_by=str(_optional_field(item, "approved_by", "ApprovedBy", default="")),
        comment=str(_optional_field(item, "comment", "Comment", default="")),
    )


def _line_from_mapping(item: dict[str, Any], *, index: int) -> ProcurementSupplierOrderLine:
    if not isinstance(item, dict):
        raise SystemExit("Every line must be an object")
    return ProcurementSupplierOrderLine(
        line_number=int(_optional_field(item, "line_number", "LineNumber", default=index) or index),
        nomenclature=_reference_from_mapping(item, "nomenclature"),
        quantity=_field(item, "quantity", "Quantity"),
        price=_field(item, "price", "Price"),
        currency=str(_optional_field(item, "currency", "Currency", default="")),
        comment=str(_optional_field(item, "comment", "Comment", default="")),
        calculation_line_id=str(
            _optional_field(item, "calculation_line_id", "CalculationLineId", default="")
        ),
        bitrix_line_id=str(_optional_field(item, "bitrix_line_id", "BitrixLineId", default="")),
    )


def _reference_from_mapping(item: dict[str, Any], prefix: str) -> OneCReference:
    nested = item.get(prefix) or item.get(_camel(prefix))
    nested = nested if isinstance(nested, dict) else {}
    return OneCReference(
        ref=str(
            _optional_field(
                nested,
                "ref",
                "Ref",
                default=_optional_field(item, f"{prefix}_ref", f"{_camel(prefix)}Ref", default=""),
            )
        ),
        code=str(
            _optional_field(
                nested,
                "code",
                "Code",
                default=_optional_field(
                    item, f"{prefix}_code", f"{_camel(prefix)}Code", default=""
                ),
            )
        ),
        name=str(
            _optional_field(
                nested,
                "name",
                "Name",
                default=_optional_field(
                    item, f"{prefix}_name", f"{_camel(prefix)}Name", default=""
                ),
            )
        ),
    )


def _camel(value: str) -> str:
    return "".join(part.capitalize() for part in value.split("_"))


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


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y", "да", "истина"}:
        return True
    if text in {"false", "0", "no", "n", "нет", "ложь"}:
        return False
    raise SystemExit(f"Invalid boolean value: {value}")


if __name__ == "__main__":
    raise SystemExit(main())
