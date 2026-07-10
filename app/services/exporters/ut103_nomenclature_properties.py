from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from xml.etree import ElementTree as ET

NOMENCLATURE_PROPERTY_UPDATES_SCHEMA = "nomenclature_property_updates.v1"
DEFAULT_SOURCE = "pricing-service"
DEFAULT_TARGET = "1c_ut_10_3"
XML_ENCODING = "windows-1251"

VALID_MODES = frozenset({"dry_run", "apply"})
VALID_TARGET_KINDS = frozenset({"property", "requisite"})
VALID_VALUE_TYPES = frozenset({"property_value", "string", "date", "number", "boolean"})


@dataclass(frozen=True)
class NomenclaturePropertyUpdateRow:
    idempotency_key: str
    nomenclature_code: str
    property_name: str
    value_type: str
    target_kind: str = "property"
    new_value: str | int | float | Decimal | bool | date | None = None
    new_value_name: str = ""
    new_value_tag: str = ""
    expected_current_value_name: str = ""
    expected_current_value_tag: str = ""
    reason: str = ""
    approved_by: str = ""


@dataclass(frozen=True)
class NomenclaturePropertyUpdateMessage:
    message_id: str
    rows: tuple[NomenclaturePropertyUpdateRow, ...]
    mode: str = "dry_run"
    approved_by: str = ""
    created_at: datetime | None = None
    source: str = DEFAULT_SOURCE
    target: str = DEFAULT_TARGET
    schema: str = NOMENCLATURE_PROPERTY_UPDATES_SCHEMA


@dataclass(frozen=True)
class PropertyUpdateItemResult:
    idempotency_key: str
    nomenclature_code: str
    property_name: str
    result: str
    message: str = ""
    current_value: str = ""
    new_value: str = ""


@dataclass(frozen=True)
class PropertyUpdateExchangeResult:
    message_id: str
    status: str
    processed_at: str
    loaded: int
    failed: int
    errors: str
    item_results: tuple[PropertyUpdateItemResult, ...] = ()
    path: Path | None = None

    @property
    def ok(self) -> bool:
        return self.status == "success" and self.failed == 0


def build_nomenclature_property_updates_xml(message: NomenclaturePropertyUpdateMessage) -> bytes:
    """Build nomenclature_property_updates.v1 XML for the UT 10.3 file exchange."""
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

    items = ET.SubElement(root, "Items")
    for row in message.rows:
        _validate_row(row, message)
        item = ET.SubElement(items, "Item")
        _add_text(item, "IdempotencyKey", row.idempotency_key)
        _add_text(item, "NomenclatureCode", row.nomenclature_code)
        if row.target_kind != "property":
            _add_text(item, "TargetKind", row.target_kind)
        _add_text(item, "PropertyName", row.property_name)
        _add_text(item, "ValueType", row.value_type)
        if row.new_value is not None:
            _add_text(item, "NewValue", _format_typed_value(row.value_type, row.new_value))
        if row.new_value_name:
            _add_text(item, "NewValueName", row.new_value_name)
        if row.new_value_tag:
            _add_text(item, "NewValueTag", row.new_value_tag)
        if row.expected_current_value_name:
            _add_text(item, "ExpectedCurrentValueName", row.expected_current_value_name)
        if row.expected_current_value_tag:
            _add_text(item, "ExpectedCurrentValueTag", row.expected_current_value_tag)
        if row.reason:
            _add_text(item, "Reason", row.reason)
        if row.approved_by:
            _add_text(item, "ApprovedBy", row.approved_by)

    ET.indent(root, space="  ")
    return ET.tostring(root, encoding=XML_ENCODING, xml_declaration=True)


def write_nomenclature_property_updates_message(
    exchange_root: str | Path,
    message: NomenclaturePropertyUpdateMessage,
    *,
    overwrite: bool = False,
) -> Path:
    """Atomically place a ready XML file into to_1c/new."""
    root = Path(exchange_root)
    new_dir = root / "to_1c" / "new"
    new_dir.mkdir(parents=True, exist_ok=True)

    filename = f"nomenclature_properties_{_safe_filename_part(message.message_id)}.ready.xml"
    target_path = new_dir / filename
    if target_path.exists() and not overwrite:
        raise FileExistsError(target_path)

    payload = build_nomenclature_property_updates_xml(message)
    tmp_path = new_dir / f"{filename}.{uuid.uuid4().hex}.tmp"
    tmp_path.write_bytes(payload)
    if overwrite and target_path.exists():
        os.replace(tmp_path, target_path)
    else:
        tmp_path.rename(target_path)
    target_path.chmod(0o660)
    return target_path


def parse_property_update_exchange_result(path: str | Path) -> PropertyUpdateExchangeResult:
    result_path = Path(path)
    root = ET.parse(result_path).getroot()
    if root.tag != "ExchangeResult":
        raise ValueError(f"unexpected root tag: {root.tag}")
    return PropertyUpdateExchangeResult(
        message_id=_node_text(root, "MessageId"),
        status=_node_text(root, "Status"),
        processed_at=_node_text(root, "ProcessedAt"),
        loaded=_parse_int(_node_text(root, "Loaded"), "Loaded"),
        failed=_parse_int(_node_text(root, "Failed"), "Failed"),
        errors=_node_text(root, "Errors"),
        item_results=tuple(
            _parse_item_result(node) for node in root.findall("ItemResults/ItemResult")
        ),
        path=result_path,
    )


def list_property_update_exchange_results(
    exchange_root: str | Path,
) -> list[PropertyUpdateExchangeResult]:
    result_dir = Path(exchange_root) / "from_1c" / "new"
    if not result_dir.exists():
        return []
    return [
        parse_property_update_exchange_result(path)
        for path in sorted(result_dir.glob("nomenclature_properties_*.result.xml"))
    ]


def _validate_message(message: NomenclaturePropertyUpdateMessage) -> None:
    if not message.message_id.strip():
        raise ValueError("message_id is required")
    if message.mode not in VALID_MODES:
        raise ValueError(f"mode must be one of: {', '.join(sorted(VALID_MODES))}")
    if not message.rows:
        raise ValueError("at least one property update row is required")
    if message.mode == "apply" and not message.approved_by.strip():
        rows_without_approval = [row for row in message.rows if not row.approved_by.strip()]
        if rows_without_approval:
            raise ValueError("apply mode requires ApprovedBy in message header or every row")


def _validate_row(
    row: NomenclaturePropertyUpdateRow,
    message: NomenclaturePropertyUpdateMessage,
) -> None:
    if not row.idempotency_key.strip():
        raise ValueError("idempotency_key is required")
    if not row.nomenclature_code.strip():
        raise ValueError("nomenclature_code is required")
    if not row.property_name.strip():
        raise ValueError("property_name is required")
    if row.target_kind not in VALID_TARGET_KINDS:
        raise ValueError(f"target_kind must be one of: {', '.join(sorted(VALID_TARGET_KINDS))}")
    if row.value_type not in VALID_VALUE_TYPES:
        raise ValueError(f"value_type must be one of: {', '.join(sorted(VALID_VALUE_TYPES))}")
    if row.target_kind == "requisite" and row.value_type == "property_value":
        raise ValueError("requisite rows cannot use property_value")
    if row.value_type == "property_value" and not (
        row.new_value_name.strip() or row.new_value_tag.strip()
    ):
        raise ValueError("property_value rows require new_value_name or new_value_tag")
    if row.value_type != "property_value" and row.new_value is None:
        raise ValueError(f"{row.value_type} rows require new_value")
    if message.mode == "apply" and not (message.approved_by.strip() or row.approved_by.strip()):
        raise ValueError("apply mode requires ApprovedBy")
    if row.new_value is not None:
        _format_typed_value(row.value_type, row.new_value)


def _parse_item_result(node: ET.Element) -> PropertyUpdateItemResult:
    return PropertyUpdateItemResult(
        idempotency_key=_node_text(node, "IdempotencyKey"),
        nomenclature_code=_node_text(node, "NomenclatureCode"),
        property_name=_node_text(node, "PropertyName"),
        result=_node_text(node, "Result") or _node_text(node, "Status"),
        message=_node_text(node, "Message"),
        current_value=_node_text(node, "CurrentValue"),
        new_value=_node_text(node, "NewValue"),
    )


def _add_text(parent: ET.Element, name: str, value: str) -> None:
    child = ET.SubElement(parent, name)
    child.text = value


def _format_created_at(value: datetime | None) -> str:
    created_at = value or datetime.now().astimezone()
    return created_at.isoformat(timespec="seconds")


def _format_typed_value(value_type: str, value: str | int | float | Decimal | bool | date) -> str:
    if value_type == "date":
        return _format_date(value)
    if value_type == "number":
        return _format_decimal(value)
    if value_type == "boolean":
        return _format_boolean(value)
    return str(value)


def _format_date(value: str | date | int | float | Decimal | bool) -> str:
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    text = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    raise ValueError(f"date values must be YYYY-MM-DD, got: {value}")


def _format_decimal(value: str | int | float | Decimal | bool | date) -> str:
    try:
        decimal_value = Decimal(str(value).replace(",", "."))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"invalid decimal value: {value}") from error
    return format(decimal_value, "f")


def _format_boolean(value: str | int | float | Decimal | bool | date) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y", "да", "истина"}:
        return "true"
    if text in {"false", "0", "no", "n", "нет", "ложь"}:
        return "false"
    raise ValueError(f"invalid boolean value: {value}")


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
