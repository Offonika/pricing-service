from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

CUSTOMER_PRICE_TYPE_UPDATES_SCHEMA = "customer_price_type_updates.v1"
DEFAULT_SOURCE = "pricing-service"
DEFAULT_TARGET = "1c_ut_10_3"
XML_ENCODING = "windows-1251"

VALID_MODES = frozenset({"dry_run", "apply"})
APPROVED_DECISION = "approved_for_manual_1c_update"
EXPECTED_CURRENT_PRICE_TYPE = "2.Бронзовый"
TARGET_PRICE_TYPE = "Розница"
_COUNTERPARTY_REF_RE = re.compile(r"^0[xX]([0-9a-fA-F]{32})$")


@dataclass(frozen=True)
class CustomerPriceTypeUpdateRow:
    idempotency_key: str
    counterparty_ref: str
    counterparty_guid: str
    counterparty_name: str
    expected_current_price_type: str
    target_price_type: str
    decision: str = APPROVED_DECISION
    reason: str = ""


@dataclass(frozen=True)
class CustomerPriceTypeUpdateMessage:
    message_id: str
    rows: tuple[CustomerPriceTypeUpdateRow, ...]
    mode: str = "dry_run"
    approved_by: str = ""
    created_at: datetime | None = None
    source: str = DEFAULT_SOURCE
    target: str = DEFAULT_TARGET
    schema: str = CUSTOMER_PRICE_TYPE_UPDATES_SCHEMA


@dataclass(frozen=True)
class CustomerPriceTypeItemResult:
    idempotency_key: str
    counterparty_ref: str
    counterparty_guid: str
    counterparty_name: str
    result: str
    message: str = ""
    contract_guid: str = ""
    contract_name: str = ""
    current_price_type: str = ""
    target_price_type: str = ""
    found_contracts: str = ""


@dataclass(frozen=True)
class CustomerPriceTypeExchangeResult:
    message_id: str
    status: str
    processed_at: str
    loaded: int
    failed: int
    errors: str
    item_results: tuple[CustomerPriceTypeItemResult, ...] = ()
    path: Path | None = None

    @property
    def ok(self) -> bool:
        return self.status == "success" and self.failed == 0


def one_c_guid_from_counterparty_ref(counterparty_ref: str) -> str:
    """Convert the SQL `_IDRRef` textual form to the GUID accepted by UT 10.3.

    The recommendation report stores links as ``0X`` + 16 little-endian bytes
    from SQL Server. ``uuid.UUID(bytes_le=...)`` mirrors the 1C GUID layout,
    allowing the 1C loader to use ``ПолучитьСсылку(Новый
    УникальныйИдентификатор(...))`` without looking up a customer by name.
    """

    matched = _COUNTERPARTY_REF_RE.fullmatch(counterparty_ref.strip())
    if matched is None:
        raise ValueError("counterparty_ref must be 0X followed by 32 hexadecimal characters")
    return str(uuid.UUID(bytes_le=bytes.fromhex(matched.group(1))))


def build_customer_price_type_updates_xml(message: CustomerPriceTypeUpdateMessage) -> bytes:
    """Build a safe customer_price_type_updates.v1 package for UT 10.3."""

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
        _add_text(item, "CounterpartyRef", row.counterparty_ref)
        _add_text(item, "CounterpartyGuid", row.counterparty_guid)
        _add_text(item, "CounterpartyName", row.counterparty_name)
        _add_text(item, "ExpectedCurrentPriceType", row.expected_current_price_type)
        _add_text(item, "TargetPriceType", row.target_price_type)
        _add_text(item, "Decision", row.decision)
        if row.reason:
            _add_text(item, "Reason", row.reason)

    ET.indent(root, space="  ")
    return ET.tostring(root, encoding=XML_ENCODING, xml_declaration=True)


def write_customer_price_type_updates_message(
    exchange_root: str | Path,
    message: CustomerPriceTypeUpdateMessage,
    *,
    overwrite: bool = False,
) -> Path:
    """Atomically place a ready price-type package into the shared outbox."""

    new_dir = Path(exchange_root) / "to_1c" / "new"
    new_dir.mkdir(parents=True, exist_ok=True)
    filename = f"customer_price_types_{_safe_filename_part(message.message_id)}.ready.xml"
    target_path = new_dir / filename
    if target_path.exists() and not overwrite:
        raise FileExistsError(target_path)

    temporary_path = new_dir / f"{filename}.{uuid.uuid4().hex}.tmp"
    temporary_path.write_bytes(build_customer_price_type_updates_xml(message))
    if overwrite and target_path.exists():
        os.replace(temporary_path, target_path)
    else:
        temporary_path.rename(target_path)
    target_path.chmod(0o660)
    return target_path


def parse_customer_price_type_exchange_result(
    path: str | Path,
) -> CustomerPriceTypeExchangeResult:
    result_path = Path(path)
    root = ET.parse(result_path).getroot()
    if root.tag != "ExchangeResult":
        raise ValueError(f"unexpected root tag: {root.tag}")
    return CustomerPriceTypeExchangeResult(
        message_id=_node_text(root, "MessageId"),
        status=_node_text(root, "Status"),
        processed_at=_node_text(root, "ProcessedAt"),
        loaded=_parse_int(_node_text(root, "Loaded"), "Loaded"),
        failed=_parse_int(_node_text(root, "Failed"), "Failed"),
        errors=_node_text(root, "Errors"),
        item_results=tuple(
            _parse_item_result(item) for item in root.findall("ItemResults/ItemResult")
        ),
        path=result_path,
    )


def list_customer_price_type_exchange_results(
    exchange_root: str | Path,
) -> list[CustomerPriceTypeExchangeResult]:
    result_dir = Path(exchange_root) / "from_1c" / "new"
    if not result_dir.exists():
        return []
    return [
        parse_customer_price_type_exchange_result(path)
        for path in sorted(result_dir.glob("customer_price_types_*.result.xml"))
    ]


def _validate_message(message: CustomerPriceTypeUpdateMessage) -> None:
    if not message.message_id.strip():
        raise ValueError("message_id is required")
    if message.schema != CUSTOMER_PRICE_TYPE_UPDATES_SCHEMA:
        raise ValueError(f"schema must be {CUSTOMER_PRICE_TYPE_UPDATES_SCHEMA}")
    if message.mode not in VALID_MODES:
        raise ValueError(f"mode must be one of: {', '.join(sorted(VALID_MODES))}")
    if not message.rows:
        raise ValueError("at least one customer price-type update row is required")
    if message.mode == "apply" and not message.approved_by.strip():
        raise ValueError("apply mode requires ApprovedBy in the message header")

    idempotency_keys: set[str] = set()
    counterparty_refs: set[str] = set()
    for row in message.rows:
        idempotency_key = row.idempotency_key.strip()
        counterparty_ref = row.counterparty_ref.strip().upper()
        if idempotency_key in idempotency_keys:
            raise ValueError(f"duplicate idempotency_key in message: {idempotency_key}")
        if counterparty_ref in counterparty_refs:
            raise ValueError(f"duplicate counterparty_ref in message: {row.counterparty_ref}")
        idempotency_keys.add(idempotency_key)
        counterparty_refs.add(counterparty_ref)


def _validate_row(row: CustomerPriceTypeUpdateRow, message: CustomerPriceTypeUpdateMessage) -> None:
    if not row.idempotency_key.strip():
        raise ValueError("idempotency_key is required")
    normalized_guid = one_c_guid_from_counterparty_ref(row.counterparty_ref)
    if row.counterparty_guid.strip().lower() != normalized_guid:
        raise ValueError("counterparty_guid does not match counterparty_ref")
    if not row.counterparty_name.strip():
        raise ValueError("counterparty_name is required for an auditable result")
    if row.decision != APPROVED_DECISION:
        raise ValueError(f"decision must be {APPROVED_DECISION}")
    if row.expected_current_price_type != EXPECTED_CURRENT_PRICE_TYPE:
        raise ValueError(f"expected_current_price_type must be {EXPECTED_CURRENT_PRICE_TYPE}")
    if row.target_price_type != TARGET_PRICE_TYPE:
        raise ValueError(f"target_price_type must be {TARGET_PRICE_TYPE}")


def _parse_item_result(node: ET.Element) -> CustomerPriceTypeItemResult:
    return CustomerPriceTypeItemResult(
        idempotency_key=_node_text(node, "IdempotencyKey"),
        counterparty_ref=_node_text(node, "CounterpartyRef"),
        counterparty_guid=_node_text(node, "CounterpartyGuid"),
        counterparty_name=_node_text(node, "CounterpartyName"),
        result=_node_text(node, "Result"),
        message=_node_text(node, "Message"),
        contract_guid=_node_text(node, "ContractGuid"),
        contract_name=_node_text(node, "ContractName"),
        current_price_type=_node_text(node, "CurrentPriceType"),
        target_price_type=_node_text(node, "TargetPriceType"),
        found_contracts=_node_text(node, "FoundContracts"),
    )


def _add_text(parent: ET.Element, name: str, value: str) -> None:
    child = ET.SubElement(parent, name)
    child.text = value


def _format_created_at(value: datetime | None) -> str:
    return (value or datetime.now().astimezone()).isoformat(timespec="seconds")


def _safe_filename_part(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip()).strip("._")
    if not safe:
        raise ValueError("message_id cannot be converted to a safe file name")
    return safe[:120]


def _node_text(root: ET.Element, tag: str) -> str:
    node = root.find(tag)
    return node.text.strip() if node is not None and node.text else ""


def _parse_int(value: str, field_name: str) -> int:
    normalized = (value or "0").replace(" ", "").replace("\xa0", "").replace("\u202f", "")
    try:
        return int(normalized)
    except ValueError as error:
        raise ValueError(f"{field_name} must be integer, got: {value}") from error
