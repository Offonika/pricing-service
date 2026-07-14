from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from xml.etree import ElementTree as ET

PROCUREMENT_ONEC_FILE_EXCHANGE_SCHEMA = "procurement_onec_file_exchange.v1"
DEFAULT_SOURCE = "pricing-service"
DEFAULT_TARGET = "1c_ut_10_3"
XML_ENCODING = "windows-1251"

VALID_MODES = frozenset({"dry_run", "apply"})
CONTOUR_ALIASES = {
    "ordinary": "Обычный",
    "обычный": "Обычный",
    "cargo": "Cargo",
    "карго": "Cargo",
    "ved_import": "ВЭД импорт",
    "ved-import": "ВЭД импорт",
    "вэд импорт": "ВЭД импорт",
    "вэдимпорт": "ВЭД импорт",
}


@dataclass(frozen=True)
class OneCReference:
    ref: str = ""
    code: str = ""
    name: str = ""


@dataclass(frozen=True)
class ProcurementSupplierOrderLine:
    line_number: int = 0
    nomenclature: OneCReference = OneCReference()
    quantity: str | int | float | Decimal = Decimal("0")
    price: str | int | float | Decimal = Decimal("0")
    currency: str = ""
    comment: str = ""
    calculation_line_id: str = ""
    bitrix_line_id: str = ""


@dataclass(frozen=True)
class ProcurementSupplierOrder:
    idempotency_key: str
    order_date: str | date
    procurement_contour: str
    supplier: OneCReference
    contract: OneCReference
    warehouse: OneCReference
    currency: str
    bitrix_item_url: str
    confirmation_id: str
    calculation_id: str
    lines: tuple[ProcurementSupplierOrderLine, ...]
    draft_only: bool = True
    approved_by: str = ""
    comment: str = ""


@dataclass(frozen=True)
class ProcurementSupplierOrderMessage:
    message_id: str
    orders: tuple[ProcurementSupplierOrder, ...]
    mode: str = "dry_run"
    approved_by: str = ""
    created_at: datetime | None = None
    source: str = DEFAULT_SOURCE
    target: str = DEFAULT_TARGET
    schema: str = PROCUREMENT_ONEC_FILE_EXCHANGE_SCHEMA


@dataclass(frozen=True)
class ProcurementSupplierOrderItemResult:
    idempotency_key: str
    result: str
    message: str = ""
    onec_document_ref: str = ""
    onec_document_number: str = ""
    onec_document_date: str = ""


@dataclass(frozen=True)
class ProcurementSupplierOrderExchangeResult:
    message_id: str
    status: str
    processed_at: str
    loaded: int
    failed: int
    errors: str
    item_results: tuple[ProcurementSupplierOrderItemResult, ...] = ()
    path: Path | None = None

    @property
    def ok(self) -> bool:
        return self.status == "success" and self.failed == 0


def build_procurement_supplier_orders_xml(message: ProcurementSupplierOrderMessage) -> bytes:
    """Build procurement_onec_file_exchange.v1 XML for UT 10.3 draft supplier orders."""
    _validate_message(message)

    root = ET.Element("ExchangeMessage")
    header = ET.SubElement(root, "Header")
    _add_text(header, "MessageId", message.message_id)
    _add_text(header, "Schema", message.schema)
    _add_text(header, "CreatedAt", _format_created_at(message.created_at))
    _add_text(header, "Source", message.source)
    _add_text(header, "Target", message.target)
    _add_text(header, "Mode", message.mode)
    if message.approved_by:
        _add_text(header, "ApprovedBy", message.approved_by)

    orders_node = ET.SubElement(root, "SupplierOrders")
    for order in message.orders:
        _validate_order(order, message)
        order_node = ET.SubElement(orders_node, "SupplierOrder")
        _add_text(order_node, "IdempotencyKey", order.idempotency_key)
        _add_text(order_node, "DraftOnly", "true")
        _add_text(order_node, "OrderDate", _format_date(order.order_date))
        _add_text(
            order_node,
            "ProcurementContour",
            _normalize_procurement_contour(order.procurement_contour),
        )
        _add_text(order_node, "Currency", order.currency)
        _add_reference(order_node, "Supplier", order.supplier)
        _add_reference(order_node, "Contract", order.contract)
        _add_reference(order_node, "Warehouse", order.warehouse)
        _add_text(order_node, "BitrixItemUrl", order.bitrix_item_url)
        _add_text(order_node, "ConfirmationId", order.confirmation_id)
        _add_text(order_node, "CalculationId", order.calculation_id)
        if order.approved_by:
            _add_text(order_node, "ApprovedBy", order.approved_by)
        if order.comment:
            _add_text(order_node, "Comment", order.comment)

        lines_node = ET.SubElement(order_node, "Lines")
        for index, line in enumerate(order.lines, start=1):
            _validate_line(line, index=index, order_currency=order.currency)
            line_node = ET.SubElement(lines_node, "Line")
            _add_text(line_node, "LineNumber", str(line.line_number or index))
            _add_reference(line_node, "Nomenclature", line.nomenclature)
            _add_text(line_node, "Quantity", _format_decimal(line.quantity))
            _add_text(line_node, "Price", _format_decimal(line.price))
            _add_text(line_node, "Currency", line.currency or order.currency)
            if line.calculation_line_id:
                _add_text(line_node, "CalculationLineId", line.calculation_line_id)
            if line.bitrix_line_id:
                _add_text(line_node, "BitrixLineId", line.bitrix_line_id)
            if line.comment:
                _add_text(line_node, "Comment", line.comment)

    ET.indent(root, space="  ")
    return ET.tostring(root, encoding=XML_ENCODING, xml_declaration=True)


def write_procurement_supplier_orders_message(
    exchange_root: str | Path,
    message: ProcurementSupplierOrderMessage,
    *,
    overwrite: bool = False,
) -> Path:
    """Atomically place a supplier-order ready XML file into to_1c/new."""
    root = Path(exchange_root)
    new_dir = root / "to_1c" / "new"
    new_dir.mkdir(parents=True, exist_ok=True)

    filename = f"procurement_supplier_orders_{_safe_filename_part(message.message_id)}.ready.xml"
    target_path = new_dir / filename
    if target_path.exists() and not overwrite:
        raise FileExistsError(target_path)

    payload = build_procurement_supplier_orders_xml(message)
    tmp_path = new_dir / f"{filename}.{uuid.uuid4().hex}.tmp"
    tmp_path.write_bytes(payload)
    if overwrite and target_path.exists():
        os.replace(tmp_path, target_path)
    else:
        tmp_path.rename(target_path)
    target_path.chmod(0o660)
    return target_path


def parse_procurement_supplier_order_exchange_result(
    path: str | Path,
) -> ProcurementSupplierOrderExchangeResult:
    result_path = Path(path)
    root = ET.parse(result_path).getroot()
    if root.tag != "ExchangeResult":
        raise ValueError(f"unexpected root tag: {root.tag}")
    return ProcurementSupplierOrderExchangeResult(
        message_id=_node_text(root, "MessageId"),
        status=_node_text(root, "Status"),
        processed_at=_node_text(root, "ProcessedAt"),
        loaded=_parse_int(_node_text(root, "Loaded"), "Loaded"),
        failed=_parse_int(_node_text(root, "Failed"), "Failed"),
        errors=_node_text(root, "Errors"),
        item_results=tuple(
            _parse_item_result(node)
            for node in (
                root.findall("OrderResults/OrderResult")
                or root.findall("DocumentResults/DocumentResult")
            )
        ),
        path=result_path,
    )


def list_procurement_supplier_order_exchange_results(
    exchange_root: str | Path,
) -> list[ProcurementSupplierOrderExchangeResult]:
    result_dir = Path(exchange_root) / "from_1c" / "new"
    if not result_dir.exists():
        return []
    return [
        parse_procurement_supplier_order_exchange_result(path)
        for path in sorted(result_dir.glob("procurement_supplier_orders_*.result.xml"))
    ]


def _validate_message(message: ProcurementSupplierOrderMessage) -> None:
    if not message.message_id.strip():
        raise ValueError("message_id is required")
    if message.mode not in VALID_MODES:
        raise ValueError(f"mode must be one of: {', '.join(sorted(VALID_MODES))}")
    if not message.orders:
        raise ValueError("at least one supplier order is required")
    if message.mode == "apply" and not message.approved_by.strip():
        orders_without_approval = [
            order for order in message.orders if not order.approved_by.strip()
        ]
        if orders_without_approval:
            raise ValueError("apply mode requires ApprovedBy in message header or every order")


def _validate_order(
    order: ProcurementSupplierOrder,
    message: ProcurementSupplierOrderMessage,
) -> None:
    if not order.idempotency_key.strip():
        raise ValueError("idempotency_key is required")
    if not order.draft_only:
        raise ValueError("draft_only must be true")
    _format_date(order.order_date)
    _normalize_procurement_contour(order.procurement_contour)
    if not order.currency.strip():
        raise ValueError("currency is required")
    _validate_reference(order.supplier, "supplier")
    _validate_reference(order.contract, "contract")
    _validate_reference(order.warehouse, "warehouse")
    if not order.bitrix_item_url.strip():
        raise ValueError("bitrix_item_url is required")
    if not order.confirmation_id.strip():
        raise ValueError("confirmation_id is required")
    if not order.calculation_id.strip():
        raise ValueError("calculation_id is required")
    if not order.lines:
        raise ValueError("at least one supplier order line is required")
    if message.mode == "apply" and not (message.approved_by.strip() or order.approved_by.strip()):
        raise ValueError("apply mode requires ApprovedBy")


def _validate_line(
    line: ProcurementSupplierOrderLine,
    *,
    index: int,
    order_currency: str,
) -> None:
    if line.line_number < 0:
        raise ValueError("line_number cannot be negative")
    _validate_reference(line.nomenclature, f"line {index} nomenclature")
    if _decimal(line.quantity) <= 0:
        raise ValueError(f"line {index} quantity must be positive")
    if _decimal(line.price) <= 0:
        raise ValueError(f"line {index} price must be positive")
    if not (line.currency or order_currency).strip():
        raise ValueError(f"line {index} currency is required")


def _validate_reference(value: OneCReference, label: str) -> None:
    if not (value.ref.strip() or value.code.strip()):
        raise ValueError(f"{label} ref or code is required")


def _parse_item_result(node: ET.Element) -> ProcurementSupplierOrderItemResult:
    return ProcurementSupplierOrderItemResult(
        idempotency_key=_node_text(node, "IdempotencyKey"),
        result=_node_text(node, "Result") or _node_text(node, "Status"),
        message=_node_text(node, "Message"),
        onec_document_ref=_node_text(node, "OnecDocumentRef"),
        onec_document_number=_node_text(node, "OnecDocumentNumber"),
        onec_document_date=_node_text(node, "OnecDocumentDate"),
    )


def _add_reference(parent: ET.Element, name: str, value: OneCReference) -> None:
    node = ET.SubElement(parent, name)
    if value.ref:
        _add_text(node, "Ref", value.ref)
    if value.code:
        _add_text(node, "Code", value.code)
    if value.name:
        _add_text(node, "Name", value.name)


def _add_text(parent: ET.Element, name: str, value: str) -> None:
    child = ET.SubElement(parent, name)
    child.text = str(value)


def _format_created_at(value: datetime | None) -> str:
    created_at = value or datetime.now().astimezone()
    return created_at.isoformat(timespec="seconds")


def _format_date(value: str | date) -> str:
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    text = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    raise ValueError(f"date values must be YYYY-MM-DD, got: {value}")


def _format_decimal(value: str | int | float | Decimal) -> str:
    return format(_decimal(value), "f")


def _decimal(value: str | int | float | Decimal) -> Decimal:
    try:
        return Decimal(str(value).replace(",", "."))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"invalid decimal value: {value}") from error


def _normalize_procurement_contour(value: str) -> str:
    text = value.strip()
    normalized = CONTOUR_ALIASES.get(text.casefold())
    if normalized:
        return normalized
    if text in set(CONTOUR_ALIASES.values()):
        return text
    raise ValueError("procurement_contour must be one of: Обычный, Cargo, ВЭД импорт")


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
    normalized = (value or "0").replace(" ", "").replace("\xa0", "").replace("\u202f", "")
    try:
        return int(normalized)
    except ValueError as error:
        raise ValueError(f"{field_name} must be integer, got: {value}") from error
