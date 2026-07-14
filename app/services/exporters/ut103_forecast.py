from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from xml.etree import ElementTree as ET

FORECAST_SCHEMA = "forecast_sales.v1"
DEFAULT_SOURCE = "pricing-service"
DEFAULT_TARGET = "1c_ut_10_3"
XML_ENCODING = "windows-1251"


@dataclass(frozen=True)
class ForecastSalesRow:
    nomenclature_code: str
    warehouse_code: str
    period: str | date
    forecast_qty: Decimal | int | float | str
    forecast_amount: Decimal | int | float | str = Decimal("0")


@dataclass(frozen=True)
class ForecastSalesMessage:
    message_id: str
    rows: tuple[ForecastSalesRow, ...]
    created_at: datetime | None = None
    source: str = DEFAULT_SOURCE
    target: str = DEFAULT_TARGET
    schema: str = FORECAST_SCHEMA


@dataclass(frozen=True)
class ExchangeResult:
    message_id: str
    status: str
    processed_at: str
    loaded: int
    failed: int
    errors: str
    path: Path | None = None

    @property
    def ok(self) -> bool:
        return self.status == "success" and self.failed == 0


def build_forecast_sales_xml(message: ForecastSalesMessage) -> bytes:
    """Build forecast_sales.v1 XML for the 1C UT 10.3 file exchange."""
    if not message.message_id.strip():
        raise ValueError("message_id is required")
    if not message.rows:
        raise ValueError("at least one forecast row is required")

    root = ET.Element("ExchangeMessage")
    header = ET.SubElement(root, "Header")
    _add_text(header, "MessageId", message.message_id)
    _add_text(header, "Schema", message.schema)
    _add_text(header, "CreatedAt", _format_created_at(message.created_at))
    _add_text(header, "Source", message.source)
    _add_text(header, "Target", message.target)

    items = ET.SubElement(root, "Items")
    for row in message.rows:
        _validate_row(row)
        item = ET.SubElement(items, "Item")
        _add_text(item, "NomenclatureCode", row.nomenclature_code)
        _add_text(item, "WarehouseCode", row.warehouse_code)
        _add_text(item, "Period", _format_period(row.period))
        _add_text(item, "ForecastQty", _format_decimal(row.forecast_qty))
        _add_text(item, "ForecastAmount", _format_decimal(row.forecast_amount))

    ET.indent(root, space="  ")
    return ET.tostring(root, encoding=XML_ENCODING, xml_declaration=True)


def write_forecast_sales_message(
    exchange_root: str | Path,
    message: ForecastSalesMessage,
    *,
    overwrite: bool = False,
) -> Path:
    """Atomically place a ready XML file into to_1c/new."""
    root = Path(exchange_root)
    new_dir = root / "to_1c" / "new"
    new_dir.mkdir(parents=True, exist_ok=True)

    filename = f"forecast_sales_{_safe_filename_part(message.message_id)}.ready.xml"
    target_path = new_dir / filename
    if target_path.exists() and not overwrite:
        raise FileExistsError(target_path)

    payload = build_forecast_sales_xml(message)
    tmp_path = new_dir / f"{filename}.{uuid.uuid4().hex}.tmp"
    tmp_path.write_bytes(payload)
    if overwrite and target_path.exists():
        os.replace(tmp_path, target_path)
    else:
        tmp_path.rename(target_path)
    return target_path


def parse_exchange_result(path: str | Path) -> ExchangeResult:
    result_path = Path(path)
    root = ET.parse(result_path).getroot()
    if root.tag != "ExchangeResult":
        raise ValueError(f"unexpected root tag: {root.tag}")
    return ExchangeResult(
        message_id=_node_text(root, "MessageId"),
        status=_node_text(root, "Status"),
        processed_at=_node_text(root, "ProcessedAt"),
        loaded=_parse_int(_node_text(root, "Loaded"), "Loaded"),
        failed=_parse_int(_node_text(root, "Failed"), "Failed"),
        errors=_node_text(root, "Errors"),
        path=result_path,
    )


def list_exchange_results(exchange_root: str | Path) -> list[ExchangeResult]:
    result_dir = Path(exchange_root) / "from_1c" / "new"
    if not result_dir.exists():
        return []
    return [parse_exchange_result(path) for path in sorted(result_dir.glob("*.result.xml"))]


def _add_text(parent: ET.Element, name: str, value: str) -> None:
    child = ET.SubElement(parent, name)
    child.text = value


def _validate_row(row: ForecastSalesRow) -> None:
    if not row.nomenclature_code.strip():
        raise ValueError("nomenclature_code is required")
    if not row.warehouse_code.strip():
        raise ValueError("warehouse_code is required")
    _format_period(row.period)
    _format_decimal(row.forecast_qty)
    _format_decimal(row.forecast_amount)


def _format_created_at(value: datetime | None) -> str:
    created_at = value or datetime.now().astimezone()
    return created_at.isoformat(timespec="seconds")


def _format_period(value: str | date) -> str:
    if isinstance(value, date):
        return value.strftime("%Y-%m")
    period = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}", period):
        return period
    raise ValueError(f"period must be YYYY-MM, got: {value}")


def _format_decimal(value: Decimal | int | float | str) -> str:
    try:
        decimal_value = Decimal(str(value).replace(",", "."))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"invalid decimal value: {value}") from error
    return format(decimal_value, "f")


def _safe_filename_part(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    safe = safe.strip("._")
    if not safe:
        raise ValueError("message_id cannot be converted to a safe file name")
    return safe[:120]


def _node_text(root: ET.Element, tag: str) -> str:
    node = root.find(tag)
    if node is None or node.text is None:
        return ""
    return node.text.strip()


def _parse_int(value: str, field_name: str) -> int:
    try:
        return int(value or "0")
    except ValueError as error:
        raise ValueError(f"{field_name} must be integer, got: {value}") from error
