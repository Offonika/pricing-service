from __future__ import annotations

import hashlib
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from xml.etree import ElementTree as ET

from app.services.exporters.ut103_customer_price_types import (
    one_c_guid_from_counterparty_ref,
)

ONEC_COMMANDS_SCHEMA = "onec_commands.v1"
SET_CREDIT_TERMS_COMMAND = "set_credit_terms"
DEFAULT_SOURCE = "pricing-service"
DEFAULT_TARGET = "1c_ut_10_3"
XML_ENCODING = "windows-1251"
MAX_MESSAGE_ID_LENGTH = 120
MAX_CREDIT_LIMIT = Decimal("9999999999999999.99")
MAX_CREDIT_DEPTH = 99999
VALID_MODES = frozenset({"dry_run", "apply"})
VALID_RESULT_STATUSES = frozenset(
    {"validated", "applied", "already_actual", "needs_review", "failed"}
)
_DECISION_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_MESSAGE_ID_RE = re.compile(
    r"^rcd-(?P<entity>[0-9]+)-(?P<item>[0-9]+)-"
    r"(?P<revision>[0-9a-f]{12})-(?P<decision>[0-9a-f]{12})-"
    r"(?P<suffix>dry-run|apply|readback)$"
)


@dataclass(frozen=True)
class CreditTermsCommand:
    idempotency_key: str
    decision_id: str
    decision_hash: str
    revision: str
    counterparty_ref: str
    counterparty_guid: str
    counterparty_code: str
    counterparty_name: str
    contract_ref: str
    contract_guid: str
    contract_code: str
    contract_name: str
    contract_organization_ref: str
    contract_organization_guid: str
    expected_current_limit: Decimal
    expected_current_depth: int
    expected_control_enabled: bool
    new_limit: Decimal
    new_depth: int
    new_control_enabled: bool
    reason: str
    approved_by: str
    approved_at: datetime
    currency: str = "RUB"
    command_type: str = SET_CREDIT_TERMS_COMMAND


@dataclass(frozen=True)
class CreditTermsMessage:
    message_id: str
    commands: tuple[CreditTermsCommand, ...]
    mode: str = "dry_run"
    created_at: datetime | None = None
    source: str = DEFAULT_SOURCE
    target: str = DEFAULT_TARGET
    schema: str = ONEC_COMMANDS_SCHEMA


@dataclass(frozen=True)
class CreditTermsCommandResult:
    idempotency_key: str
    decision_id: str
    decision_hash: str
    counterparty_ref: str
    counterparty_guid: str
    counterparty_code: str
    contract_ref: str
    contract_guid: str
    contract_code: str
    status: str
    message: str
    old_limit: Decimal | None
    old_depth: int | None
    requested_limit: Decimal | None
    requested_depth: int | None
    readback_limit: Decimal | None
    readback_depth: int | None
    readback_control_enabled: bool | None

    @property
    def ok(self) -> bool:
        return self.status in {"validated", "applied", "already_actual"}


@dataclass(frozen=True)
class CreditTermsExchangeResult:
    message_id: str
    schema: str
    status: str
    processed_at: str
    loaded: int
    failed: int
    errors: str
    command_results: tuple[CreditTermsCommandResult, ...] = ()
    path: Path | None = None

    @property
    def ok(self) -> bool:
        return (
            self.schema == ONEC_COMMANDS_SCHEMA
            and self.status == "success"
            and self.failed == 0
            and all(item.ok for item in self.command_results)
        )


def build_credit_terms_xml(message: CreditTermsMessage) -> bytes:
    """Build a validated ``onec_commands.v1`` credit-terms package."""

    _validate_message(message)
    root = ET.Element("ExchangeMessage")
    header = ET.SubElement(root, "Header")
    _add_text(header, "MessageId", message.message_id)
    _add_text(header, "Schema", message.schema)
    _add_text(header, "CreatedAt", _format_datetime(message.created_at))
    _add_text(header, "Source", message.source)
    _add_text(header, "Target", message.target)
    _add_text(header, "Mode", message.mode)

    commands = ET.SubElement(root, "Commands")
    for command in message.commands:
        _validate_command(command, mode=message.mode)
        node = ET.SubElement(commands, "Command")
        _add_text(node, "IdempotencyKey", command.idempotency_key)
        _add_text(node, "CommandType", command.command_type)
        _add_text(node, "DecisionId", command.decision_id)
        _add_text(node, "DecisionHash", command.decision_hash)
        _add_text(node, "ReportRevision", command.revision)
        _add_text(node, "CounterpartyRef", command.counterparty_ref)
        _add_text(node, "CounterpartyGuid", command.counterparty_guid)
        _add_text(node, "CounterpartyCode", command.counterparty_code)
        _add_text(node, "CounterpartyName", command.counterparty_name)
        _add_text(node, "ContractRef", command.contract_ref)
        _add_text(node, "ContractGuid", command.contract_guid)
        _add_text(node, "ContractCode", command.contract_code)
        _add_text(node, "ContractName", command.contract_name)
        _add_text(node, "ContractOrganizationRef", command.contract_organization_ref)
        _add_text(node, "ContractOrganizationGuid", command.contract_organization_guid)
        _add_text(node, "ExpectedCurrentLimit", _format_decimal(command.expected_current_limit))
        _add_text(node, "ExpectedCurrentDepth", str(command.expected_current_depth))
        _add_text(node, "ExpectedControlEnabled", _format_bool(command.expected_control_enabled))
        _add_text(node, "NewLimit", _format_decimal(command.new_limit))
        _add_text(node, "NewDepth", str(command.new_depth))
        _add_text(node, "NewControlEnabled", _format_bool(command.new_control_enabled))
        _add_text(node, "Currency", command.currency)
        _add_text(node, "Reason", command.reason)
        _add_text(node, "ApprovedBy", command.approved_by)
        _add_text(node, "ApprovedAt", _format_datetime(command.approved_at))

    ET.indent(root, space="  ")
    return ET.tostring(root, encoding=XML_ENCODING, xml_declaration=True)


def write_credit_terms_message(
    exchange_root: str | Path,
    message: CreditTermsMessage,
    *,
    overwrite: bool = False,
) -> Path:
    """Atomically publish a ready message into the existing UT 10.3 outbox."""

    new_dir = Path(exchange_root) / "to_1c" / "new"
    new_dir.mkdir(parents=True, exist_ok=True)
    filename = f"onec_commands_{_safe_filename_part(message.message_id)}.ready.xml"
    target_path = new_dir / filename
    if target_path.exists() and not overwrite:
        raise FileExistsError(target_path)

    temporary_path = new_dir / f"{filename}.{uuid.uuid4().hex}.tmp"
    temporary_path.write_bytes(build_credit_terms_xml(message))
    if overwrite and target_path.exists():
        os.replace(temporary_path, target_path)
    else:
        temporary_path.rename(target_path)
    target_path.chmod(0o660)
    return target_path


def parse_credit_terms_result(path: str | Path) -> CreditTermsExchangeResult:
    result_path = Path(path)
    root = ET.parse(result_path).getroot()
    if root.tag != "ExchangeResult":
        raise ValueError(f"unexpected root tag: {root.tag}")
    schema = _node_text(root, "Schema")
    if schema != ONEC_COMMANDS_SCHEMA:
        raise ValueError(f"unexpected result schema: {schema}")
    command_results = tuple(
        _parse_command_result(node) for node in root.findall("CommandResults/CommandResult")
    )
    return CreditTermsExchangeResult(
        message_id=_required_node_text(root, "MessageId"),
        schema=schema,
        status=_required_node_text(root, "Status"),
        processed_at=_node_text(root, "ProcessedAt"),
        loaded=_parse_int(_node_text(root, "Loaded") or "0", "Loaded"),
        failed=_parse_int(_node_text(root, "Failed") or "0", "Failed"),
        errors=_node_text(root, "Errors"),
        command_results=command_results,
        path=result_path,
    )


def list_credit_terms_results(exchange_root: str | Path) -> list[CreditTermsExchangeResult]:
    result_dir = Path(exchange_root) / "from_1c" / "new"
    if not result_dir.exists():
        return []
    return [
        parse_credit_terms_result(path)
        for path in sorted(result_dir.glob("onec_commands_*.result.xml"))
    ]


def result_path_for_message(exchange_root: str | Path, message_id: str) -> Path:
    return (
        Path(exchange_root)
        / "from_1c"
        / "new"
        / f"onec_commands_{_safe_filename_part(message_id)}.result.xml"
    )


def build_receivable_credit_decision_message_id(
    *,
    entity_type_id: int,
    item_id: str,
    revision: str,
    decision_hash: str,
    suffix: str,
) -> str:
    entity = str(entity_type_id)
    item = str(item_id)
    if not entity.isascii() or not entity.isdigit() or int(entity) <= 0 or int(entity) > 2147483647:
        raise ValueError("entity_type_id must be a positive integer")
    if not item.isascii() or not item.isdigit() or int(item) <= 0:
        raise ValueError("item_id must be numeric")
    if len(item) > 64:
        raise ValueError("item_id must not exceed 64 characters")
    if not revision:
        raise ValueError("revision is required")
    if len(revision) > 96:
        raise ValueError("revision must not exceed 96 characters")
    if not _DECISION_HASH_RE.fullmatch(decision_hash):
        raise ValueError("decision_hash must be a lowercase SHA-256 hex digest")
    if suffix not in {"dry-run", "apply", "readback"}:
        raise ValueError("message id suffix must be dry-run, apply, or readback")
    revision_hash = hashlib.sha256(revision.encode("utf-8")).hexdigest()[:12]
    return validate_credit_terms_message_id(
        f"rcd-{entity}-{item}-{revision_hash}-{decision_hash[:12]}-{suffix}"
    )


def _validate_message(message: CreditTermsMessage) -> None:
    validate_credit_terms_message_id(message.message_id)
    if message.schema != ONEC_COMMANDS_SCHEMA:
        raise ValueError(f"schema must be {ONEC_COMMANDS_SCHEMA}")
    if message.mode not in VALID_MODES:
        raise ValueError(f"mode must be one of: {', '.join(sorted(VALID_MODES))}")
    if not message.commands:
        raise ValueError("at least one credit-terms command is required")
    if len(message.commands) != 1:
        raise ValueError("credit-terms message must contain exactly one command")
    idempotency_keys: set[str] = set()
    counterparty_keys: set[str] = set()
    for command in message.commands:
        _validate_command(command, mode=message.mode)
        idempotency_key = command.idempotency_key.strip()
        counterparty_key = command.counterparty_guid.strip().lower()
        if idempotency_key in idempotency_keys:
            raise ValueError(f"duplicate idempotency_key in message: {idempotency_key}")
        if counterparty_key in counterparty_keys:
            raise ValueError(f"duplicate counterparty in message: {command.counterparty_guid}")
        idempotency_keys.add(idempotency_key)
        counterparty_keys.add(counterparty_key)
    _validate_message_id_for_command(
        message.message_id,
        message.commands[0],
        mode=message.mode,
    )


def _validate_command(command: CreditTermsCommand, *, mode: str) -> None:
    required = {
        "idempotency_key": command.idempotency_key,
        "decision_id": command.decision_id,
        "revision": command.revision,
        "counterparty_ref": command.counterparty_ref,
        "counterparty_guid": command.counterparty_guid,
        "counterparty_code": command.counterparty_code,
        "counterparty_name": command.counterparty_name,
        "contract_ref": command.contract_ref,
        "contract_guid": command.contract_guid,
        "contract_code": command.contract_code,
        "contract_name": command.contract_name,
        "contract_organization_ref": command.contract_organization_ref,
        "contract_organization_guid": command.contract_organization_guid,
        "reason": command.reason,
        "approved_by": command.approved_by,
    }
    missing = [name for name, value in required.items() if not str(value).strip()]
    if missing:
        raise ValueError(f"required command fields are empty: {', '.join(missing)}")
    length_limits = {
        "idempotency_key": (command.idempotency_key, 200),
        "decision_id": (command.decision_id, 64),
        "revision": (command.revision, 96),
        "counterparty_ref": (command.counterparty_ref, 64),
        "counterparty_guid": (command.counterparty_guid, 36),
        "counterparty_code": (command.counterparty_code, 32),
        "counterparty_name": (command.counterparty_name, 255),
        "contract_ref": (command.contract_ref, 64),
        "contract_guid": (command.contract_guid, 36),
        "contract_code": (command.contract_code, 32),
        "contract_name": (command.contract_name, 255),
        "contract_organization_ref": (command.contract_organization_ref, 64),
        "contract_organization_guid": (command.contract_organization_guid, 36),
        "approved_by": (command.approved_by, 32),
    }
    too_long = [
        f"{name}>{limit}"
        for name, (value, limit) in length_limits.items()
        if len(value.strip()) > limit
    ]
    if too_long:
        raise ValueError(f"command fields exceed length limits: {', '.join(too_long)}")
    if command.command_type != SET_CREDIT_TERMS_COMMAND:
        raise ValueError(f"command_type must be {SET_CREDIT_TERMS_COMMAND}")
    if not command.decision_id.isascii() or not command.decision_id.isdigit():
        raise ValueError("decision_id must be numeric")
    if not _DECISION_HASH_RE.fullmatch(command.decision_hash):
        raise ValueError("decision_hash must be a lowercase SHA-256 hex digest")
    expected_guid = one_c_guid_from_counterparty_ref(command.counterparty_ref)
    if command.counterparty_guid.strip().lower() != expected_guid:
        raise ValueError("counterparty_guid does not match counterparty_ref")
    expected_contract_guid = one_c_guid_from_counterparty_ref(command.contract_ref)
    if command.contract_guid.strip().lower() != expected_contract_guid:
        raise ValueError("contract_guid does not match contract_ref")
    expected_organization_guid = one_c_guid_from_counterparty_ref(command.contract_organization_ref)
    if command.contract_organization_guid.strip().lower() != expected_organization_guid:
        raise ValueError("contract_organization_guid does not match contract_organization_ref")
    if command.currency != "RUB":
        raise ValueError("currency must be RUB")
    _validate_money(command.expected_current_limit, "expected_current_limit")
    _validate_money(command.new_limit, "new_limit")
    _validate_depth(command.expected_current_depth, "expected_current_depth")
    _validate_depth(command.new_depth, "new_depth")
    if command.approved_at.tzinfo is None:
        raise ValueError("approved_at must include timezone")
    if mode == "apply" and not command.approved_by.strip():
        raise ValueError("apply mode requires ApprovedBy")


def _validate_money(value: Decimal, field_name: str) -> None:
    if not isinstance(value, Decimal):
        raise ValueError(f"{field_name} must be Decimal")
    if not value.is_finite():
        raise ValueError(f"{field_name} must be finite")
    if value < 0:
        raise ValueError(f"{field_name} must not be negative")
    if value > MAX_CREDIT_LIMIT:
        raise ValueError(f"{field_name} exceeds Numeric(18,2)")
    if value.as_tuple().exponent < -2:
        raise ValueError(f"{field_name} must have at most 2 decimal places")


def _validate_depth(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer")
    if value < 0:
        raise ValueError(f"{field_name} must not be negative")
    if value > MAX_CREDIT_DEPTH:
        raise ValueError(f"{field_name} exceeds Numeric(5,0)")


def _parse_command_result(node: ET.Element) -> CreditTermsCommandResult:
    status = _required_node_text(node, "Status")
    if status not in VALID_RESULT_STATUSES:
        raise ValueError(f"unsupported command result status: {status}")
    return CreditTermsCommandResult(
        idempotency_key=_required_node_text(node, "IdempotencyKey"),
        decision_id=_node_text(node, "DecisionId"),
        decision_hash=_node_text(node, "DecisionHash"),
        counterparty_ref=_node_text(node, "CounterpartyRef"),
        counterparty_guid=_node_text(node, "CounterpartyGuid"),
        counterparty_code=_node_text(node, "CounterpartyCode"),
        contract_ref=_node_text(node, "ContractRef"),
        contract_guid=_node_text(node, "ContractGuid"),
        contract_code=_node_text(node, "ContractCode"),
        status=status,
        message=_node_text(node, "Message"),
        old_limit=_optional_decimal(_node_text(node, "OldLimit"), "OldLimit"),
        old_depth=_optional_int(_node_text(node, "OldDepth"), "OldDepth"),
        requested_limit=_optional_decimal(_node_text(node, "RequestedLimit"), "RequestedLimit"),
        requested_depth=_optional_int(_node_text(node, "RequestedDepth"), "RequestedDepth"),
        readback_limit=_optional_decimal(_node_text(node, "ReadbackLimit"), "ReadbackLimit"),
        readback_depth=_optional_int(_node_text(node, "ReadbackDepth"), "ReadbackDepth"),
        readback_control_enabled=_optional_bool(
            _node_text(node, "ReadbackControlEnabled"), "ReadbackControlEnabled"
        ),
    )


def _add_text(parent: ET.Element, name: str, value: str) -> None:
    node = ET.SubElement(parent, name)
    node.text = value


def _format_datetime(value: datetime | None) -> str:
    current = value or datetime.now().astimezone()
    if current.tzinfo is None:
        raise ValueError("datetime must include timezone")
    return current.isoformat(timespec="seconds")


def _format_decimal(value: Decimal) -> str:
    return format(value, "f")


def _format_bool(value: bool) -> str:
    if not isinstance(value, bool):
        raise ValueError("control flag must be boolean")
    return "true" if value else "false"


def _safe_filename_part(value: str) -> str:
    return validate_credit_terms_message_id(value)


def validate_credit_terms_message_id(value: str) -> str:
    message_id = value
    if not message_id:
        raise ValueError("message_id is required")
    if message_id != message_id.strip():
        raise ValueError("message_id must not contain leading or trailing whitespace")
    if len(message_id) > MAX_MESSAGE_ID_LENGTH:
        raise ValueError(f"message_id must not exceed {MAX_MESSAGE_ID_LENGTH} characters")
    if not _MESSAGE_ID_RE.fullmatch(message_id):
        raise ValueError(
            "message_id must match "
            "rcd-<entity>-<item>-<revision_hash12>-<decision_hash12>-<suffix>"
        )
    return message_id


def _validate_message_id_for_command(
    message_id: str,
    command: CreditTermsCommand,
    *,
    mode: str,
) -> None:
    match = _MESSAGE_ID_RE.fullmatch(message_id)
    if match is None:
        raise ValueError("message_id does not match the credit-decision contract")
    expected_revision = hashlib.sha256(command.revision.encode("utf-8")).hexdigest()[:12]
    allowed_suffixes = {"apply"} if mode == "apply" else {"dry-run", "readback"}
    if (
        match.group("item") != command.decision_id
        or match.group("revision") != expected_revision
        or match.group("decision") != command.decision_hash[:12]
        or match.group("suffix") not in allowed_suffixes
    ):
        raise ValueError("message_id does not match command identity or mode")


def _node_text(root: ET.Element, tag: str) -> str:
    node = root.find(tag)
    return node.text.strip() if node is not None and node.text else ""


def _required_node_text(root: ET.Element, tag: str) -> str:
    value = _node_text(root, tag)
    if not value:
        raise ValueError(f"missing required result field: {tag}")
    return value


def _parse_int(value: str, field_name: str) -> int:
    try:
        return int(value.replace(" ", "").replace("\xa0", ""))
    except ValueError as error:
        raise ValueError(f"{field_name} must be integer, got: {value}") from error


def _optional_int(value: str, field_name: str) -> int | None:
    return None if value == "" else _parse_int(value, field_name)


def _optional_decimal(value: str, field_name: str) -> Decimal | None:
    if value == "":
        return None
    try:
        parsed = Decimal(value.replace(" ", "").replace("\xa0", "").replace(",", "."))
    except InvalidOperation as error:
        raise ValueError(f"{field_name} must be decimal, got: {value}") from error
    if not parsed.is_finite():
        raise ValueError(f"{field_name} must be finite")
    return parsed


def _optional_bool(value: str, field_name: str) -> bool | None:
    if value == "":
        return None
    normalized = value.strip().lower()
    if normalized in {"true", "1"}:
        return True
    if normalized in {"false", "0"}:
        return False
    raise ValueError(f"{field_name} must be boolean, got: {value}")
