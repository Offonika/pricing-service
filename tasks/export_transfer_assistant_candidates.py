from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence

from app.core.config import get_settings
from app.services import transfer_assistant

CSV_COLUMNS = [
    "status",
    "reason",
    "quantity",
    "product_code",
    "product_name",
    "product_ref",
    "warehouse_code",
    "warehouse_name",
    "warehouse_ref",
    "order_number",
    "site_order_number",
    "order_ref",
    "source_document_type",
    "source_document_number",
    "source_document_ref",
    "fact_date",
    "data_source",
    "pickup_deadline",
    "pickup_deadline_source",
    "stock_quantity",
    "reserved_quantity",
    "placement_quantity",
    "order_quantity",
    "issued_quantity",
    "return_quantity",
    "onec_document_keys",
]


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    output_format = _resolve_format(args.format, args.output)
    candidates = load_candidates(args)
    write_candidates(candidates, output_format=output_format, output_path=args.output)
    return 0


def load_candidates(args: argparse.Namespace) -> list[dict[str, Any]]:
    source_kinds = set(args.source_kind or [])
    if not source_kinds:
        return transfer_assistant.list_transfer_assistant_candidates(
            date_from=args.date_from,
            date_to=args.date_to,
            warehouse_id=args.warehouse_id,
            status=args.status,
            limit=args.limit,
        )

    if args.status == transfer_assistant.STATUS_AVAILABLE_TO_TRANSFER and not args.warehouse_id:
        raise ValueError("available_to_transfer requires warehouse_id in v1")

    bounded_limit = max(1, min(int(args.limit or 100), 1000))
    settings = get_settings()
    rows = transfer_assistant.fetch_transfer_assistant_source_rows(
        settings=settings,
        date_from=args.date_from,
        date_to=args.date_to,
        warehouse_id=args.warehouse_id,
        limit=1000 if args.status else bounded_limit,
        source_kinds=source_kinds,
    )
    candidates = transfer_assistant.build_transfer_assistant_candidates(
        rows,
        pickup_hold_days=settings.logistics_transfer_assistant_pickup_hold_days,
    )
    if args.warehouse_id:
        candidates = [
            item
            for item in candidates
            if item.warehouse.get("ref") == args.warehouse_id
            or item.warehouse.get("code") == args.warehouse_id
            or str(item.warehouse.get("id") or "") == args.warehouse_id
        ]
    if args.status:
        candidates = [item for item in candidates if item.status == args.status]
    return [item.as_dict() for item in candidates[:bounded_limit]]


def write_candidates(
    candidates: list[dict[str, Any]],
    *,
    output_format: str,
    output_path: Path | None,
) -> None:
    if output_format == "csv":
        content = build_csv(candidates)
    elif output_format == "json":
        content = json.dumps(
            [_to_jsonable(item) for item in candidates],
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        content += "\n"
    else:
        raise ValueError(f"unsupported output format: {output_format}")

    if output_path is None:
        sys.stdout.write(content)
        return

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8-sig" if output_format == "csv" else "utf-8")
    print(output_path)


def build_csv(candidates: list[dict[str, Any]]) -> str:
    rows = [_flatten_candidate(item) for item in candidates]
    from io import StringIO

    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def _flatten_candidate(candidate: dict[str, Any]) -> dict[str, str]:
    product = _dict(candidate.get("product"))
    warehouse = _dict(candidate.get("warehouse"))
    order = _dict(candidate.get("order"))
    source_document = _dict(candidate.get("source_document"))
    measures = _dict(candidate.get("measures"))
    return {
        "status": _text(candidate.get("status")),
        "reason": _text(candidate.get("reason")),
        "quantity": _text(candidate.get("quantity")),
        "product_code": _text(product.get("code")),
        "product_name": _text(product.get("name")),
        "product_ref": _text(product.get("ref")),
        "warehouse_code": _text(warehouse.get("code")),
        "warehouse_name": _text(warehouse.get("name")),
        "warehouse_ref": _text(warehouse.get("ref")),
        "order_number": _text(order.get("number")),
        "site_order_number": _text(order.get("site_order_number")),
        "order_ref": _text(order.get("ref")),
        "source_document_type": _text(source_document.get("type")),
        "source_document_number": _text(source_document.get("number")),
        "source_document_ref": _text(source_document.get("ref")),
        "fact_date": _text(candidate.get("fact_date")),
        "data_source": _text(candidate.get("data_source")),
        "pickup_deadline": _text(candidate.get("pickup_deadline")),
        "pickup_deadline_source": _text(candidate.get("pickup_deadline_source")),
        "stock_quantity": _text(measures.get("stock_quantity")),
        "reserved_quantity": _text(measures.get("reserved_quantity")),
        "placement_quantity": _text(measures.get("placement_quantity")),
        "order_quantity": _text(measures.get("order_quantity")),
        "issued_quantity": _text(measures.get("issued_quantity")),
        "return_quantity": _text(measures.get("return_quantity")),
        "onec_document_keys": json.dumps(
            _to_jsonable(candidate.get("onec_document_keys") or {}),
            ensure_ascii=False,
            sort_keys=True,
        ),
    }


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export read-only transfer assistant candidates from 1C facts."
    )
    parser.add_argument("--date-from", type=_parse_date_or_datetime)
    parser.add_argument("--date-to", type=_parse_date_or_datetime)
    parser.add_argument("--warehouse-id")
    parser.add_argument(
        "--source-kind",
        action="append",
        choices=sorted(transfer_assistant.ALL_TRANSFER_ASSISTANT_SOURCE_KINDS),
        help=(
            "Optional technical source filter for operator review exports. "
            "Can be passed multiple times."
        ),
    )
    parser.add_argument(
        "--status",
        choices=sorted(transfer_assistant.VALID_TRANSFER_ASSISTANT_STATUSES),
    )
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument(
        "--format",
        choices=("auto", "json", "csv"),
        default="auto",
        help="Output format. Auto uses the output file suffix, json for stdout.",
    )
    parser.add_argument("--output", type=Path, help="Output .json or .csv path")
    return parser.parse_args(argv)


def _resolve_format(output_format: str, output_path: Path | None) -> str:
    if output_format != "auto":
        return output_format
    if output_path and output_path.suffix.lower() == ".csv":
        return "csv"
    return "json"


def _parse_date_or_datetime(value: str) -> date | datetime:
    normalized = value.strip()
    if "T" in normalized or " " in normalized:
        return datetime.fromisoformat(normalized)
    return date.fromisoformat(normalized)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime | date):
        return value.isoformat()
    return str(value)


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_to_jsonable(item) for item in value]
    return value


if __name__ == "__main__":
    raise SystemExit(main())
