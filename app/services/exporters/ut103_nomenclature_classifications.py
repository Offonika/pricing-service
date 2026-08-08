from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

NOMENCLATURE_CLASSIFICATION_UPDATES_SCHEMA = "nomenclature_classification_updates.v3"
DEFAULT_SOURCE = "pricing-service"
DEFAULT_TARGET = "1c_ut_10_3"
XML_ENCODING = "windows-1251"

VALID_MODES = frozenset({"dry_run", "apply", "readback"})
VALID_GROUP_MODES = frozenset({"set", "clear_expected"})
VALID_CATEGORY_MODES = frozenset({"ensure_present", "replace_expected", "remove_expected"})
_HEX_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MESSAGE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")


@dataclass(frozen=True)
class OneCClassificationReference:
    guid: str = ""
    code: str = ""
    name: str = ""


@dataclass(frozen=True)
class NomenclatureClassificationIntentRow:
    idempotency_key: str
    nomenclature_code: str
    nomenclature_guid: str
    expected_kind: OneCClassificationReference
    target_kind: OneCClassificationReference
    expected_group: OneCClassificationReference
    target_group: OneCClassificationReference
    target_category: OneCClassificationReference
    group_mode: str = "set"
    category_mode: str = "ensure_present"
    expected_category: OneCClassificationReference = OneCClassificationReference()
    reason: str = ""


@dataclass(frozen=True)
class NomenclatureClassificationUpdateRow(NomenclatureClassificationIntentRow):
    decision_hash: str = ""


@dataclass(frozen=True)
class NomenclatureClassificationUpdateMessage:
    operation_id: str
    command_hash: str
    message_id: str
    rows: tuple[NomenclatureClassificationUpdateRow, ...]
    approved_by: str
    mode: str = "dry_run"
    created_at: datetime | None = None
    source: str = DEFAULT_SOURCE
    target: str = DEFAULT_TARGET
    schema: str = NOMENCLATURE_CLASSIFICATION_UPDATES_SCHEMA


@dataclass(frozen=True)
class NomenclatureClassificationItemResult:
    idempotency_key: str
    decision_hash: str
    nomenclature_code: str
    nomenclature_guid: str
    expected_kind_guid: str
    expected_kind_code: str
    target_kind_guid: str
    target_kind_code: str
    expected_group_guid: str
    expected_group_code: str
    target_group_guid: str
    target_group_code: str
    group_mode: str
    category_mode: str
    expected_category_guid: str
    expected_category_code: str
    target_category_guid: str
    target_category_code: str
    result: str
    message: str = ""
    old_kind_guid: str = ""
    readback_kind_guid: str = ""
    old_group_guid: str = ""
    readback_group_guid: str = ""
    old_category_guids: str = ""
    projected_category_guids: str = ""
    readback_category_guids: str = ""


@dataclass(frozen=True)
class NomenclatureClassificationExchangeResult:
    operation_id: str
    message_id: str
    schema: str
    mode: str
    command_hash: str
    status: str
    processed_at: str
    loaded: int
    failed: int
    errors: str
    item_results: tuple[NomenclatureClassificationItemResult, ...] = ()
    path: Path | None = None

    @property
    def ok(self) -> bool:
        return self.status == "success" and self.failed == 0


def prepare_nomenclature_classification_command(
    rows: tuple[NomenclatureClassificationIntentRow, ...],
    *,
    approved_by: str,
    source: str = DEFAULT_SOURCE,
    target: str = DEFAULT_TARGET,
) -> tuple[tuple[NomenclatureClassificationUpdateRow, ...], str, dict[str, object]]:
    """Normalize an explicit command and calculate all hashes inside the service."""

    approver = _clean_required(approved_by, "approved_by", max_length=150)
    normalized_source = _clean_required(source, "source", max_length=80)
    normalized_target = _clean_required(target, "target", max_length=80)
    if not rows:
        raise ValueError("at least one classification row is required")
    normalized = tuple(
        sorted((_normalize_intent(row) for row in rows), key=lambda row: row.idempotency_key)
    )
    _validate_unique_rows(normalized)
    prepared = tuple(
        NomenclatureClassificationUpdateRow(
            **row.__dict__,
            decision_hash=_sha256_json(
                {
                    "approved_by": approver,
                    "row": _intent_payload(row),
                    "schema": NOMENCLATURE_CLASSIFICATION_UPDATES_SCHEMA,
                    "source": normalized_source,
                    "target": normalized_target,
                }
            ),
        )
        for row in normalized
    )
    canonical: dict[str, object] = {
        "approved_by": approver,
        "items": [_row_payload(row) for row in prepared],
        "schema": NOMENCLATURE_CLASSIFICATION_UPDATES_SCHEMA,
        "source": normalized_source,
        "target": normalized_target,
    }
    return prepared, _sha256_json(canonical), canonical


def rows_from_nomenclature_classification_payload(
    payload: dict[str, object],
) -> tuple[NomenclatureClassificationUpdateRow, ...]:
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("canonical payload does not contain items")
    return tuple(_row_from_payload(item) for item in items if isinstance(item, dict))


def build_nomenclature_classification_message_id(
    operation_id: str,
    command_hash: str,
    mode: str,
) -> str:
    normalized_operation = str(uuid.UUID(operation_id)).lower()
    normalized_hash = command_hash.strip().lower()
    if not _HEX_SHA256_RE.fullmatch(normalized_hash):
        raise ValueError("command_hash must be a lowercase SHA-256 hex value")
    if mode not in VALID_MODES:
        raise ValueError(f"mode must be one of: {', '.join(sorted(VALID_MODES))}")
    suffix = "dry-run" if mode == "dry_run" else mode
    return f"ncl-{normalized_operation.replace('-', '')}-{normalized_hash[:12]}-{suffix}"


def build_nomenclature_classification_updates_xml(
    message: NomenclatureClassificationUpdateMessage,
) -> bytes:
    """Build the fail-closed v3 grouped classification command for UT 10.3."""

    _validate_message(message)
    root = ET.Element("ExchangeMessage")
    header = ET.SubElement(root, "Header")
    _add_text(header, "OperationId", message.operation_id)
    _add_text(header, "MessageId", message.message_id)
    _add_text(header, "Schema", message.schema)
    _add_text(header, "CreatedAt", _format_created_at(message.created_at))
    _add_text(header, "Source", message.source)
    _add_text(header, "Target", message.target)
    _add_text(header, "Mode", message.mode)
    _add_text(header, "CommandHash", message.command_hash)
    _add_text(header, "ApprovedBy", message.approved_by)

    items = ET.SubElement(root, "Items")
    for row in message.rows:
        item = ET.SubElement(items, "Item")
        _add_text(item, "IdempotencyKey", row.idempotency_key)
        _add_text(item, "DecisionHash", row.decision_hash)
        _add_text(item, "NomenclatureCode", row.nomenclature_code)
        _add_text(item, "NomenclatureGuid", row.nomenclature_guid)
        _add_reference(item, "ExpectedKind", row.expected_kind)
        _add_reference(item, "TargetKind", row.target_kind)
        _add_reference(item, "ExpectedGroup", row.expected_group)
        _add_reference(item, "TargetGroup", row.target_group)
        _add_text(item, "GroupMode", row.group_mode)
        _add_text(item, "CategoryMode", row.category_mode)
        _add_reference(item, "ExpectedCategory", row.expected_category)
        _add_reference(item, "TargetCategory", row.target_category)
        _add_text(item, "Reason", row.reason)

    ET.indent(root, space="  ")
    return ET.tostring(root, encoding=XML_ENCODING, xml_declaration=True)


def write_nomenclature_classification_updates_message(
    exchange_root: str | Path,
    message: NomenclatureClassificationUpdateMessage,
) -> Path:
    new_dir = Path(exchange_root) / "to_1c" / "new"
    new_dir.mkdir(parents=True, exist_ok=True)
    filename = f"nomenclature_classifications_{message.message_id}.ready.xml"
    target_path = new_dir / filename
    if target_path.exists():
        raise FileExistsError(target_path)
    temporary_path = new_dir / f"{filename}.{uuid.uuid4().hex}.tmp"
    temporary_path.write_bytes(build_nomenclature_classification_updates_xml(message))
    temporary_path.rename(target_path)
    target_path.chmod(0o660)
    return target_path


def parse_nomenclature_classification_exchange_result(
    path: str | Path,
) -> NomenclatureClassificationExchangeResult:
    result_path = Path(path)
    root = ET.parse(result_path).getroot()
    if root.tag != "ExchangeResult":
        raise ValueError(f"unexpected root tag: {root.tag}")
    schema = _required_node_text(root, "Schema")
    if schema != NOMENCLATURE_CLASSIFICATION_UPDATES_SCHEMA:
        raise ValueError(f"unexpected result schema: {schema}")
    mode = _required_node_text(root, "Mode")
    if mode not in VALID_MODES:
        raise ValueError(f"unexpected result mode: {mode}")
    result = NomenclatureClassificationExchangeResult(
        operation_id=_required_node_text(root, "OperationId"),
        message_id=_required_node_text(root, "MessageId"),
        schema=schema,
        mode=mode,
        command_hash=_required_node_text(root, "CommandHash").lower(),
        status=_required_node_text(root, "Status"),
        processed_at=_node_text(root, "ProcessedAt"),
        loaded=_parse_int(_node_text(root, "Loaded") or "0", "Loaded"),
        failed=_parse_int(_node_text(root, "Failed") or "0", "Failed"),
        errors=_node_text(root, "Errors"),
        item_results=tuple(
            _parse_item_result(node) for node in root.findall("ItemResults/ItemResult")
        ),
        path=result_path,
    )
    if not _HEX_SHA256_RE.fullmatch(result.command_hash):
        raise ValueError("result CommandHash must be lowercase SHA-256 hex")
    if not _MESSAGE_ID_RE.fullmatch(result.message_id):
        raise ValueError("result MessageId contains unsupported characters")
    return result


def list_nomenclature_classification_exchange_results(
    exchange_root: str | Path,
) -> list[NomenclatureClassificationExchangeResult]:
    result_dir = Path(exchange_root) / "from_1c" / "new"
    if not result_dir.exists():
        return []
    return [
        parse_nomenclature_classification_exchange_result(path)
        for path in sorted(result_dir.glob("nomenclature_classifications_*.result.xml"))
    ]


def validate_nomenclature_classification_result(
    message: NomenclatureClassificationUpdateMessage,
    result: NomenclatureClassificationExchangeResult,
) -> None:
    """Bind a result to one exact persisted message and validate full readback sets."""

    _validate_message(message)
    expected_header = (
        message.operation_id,
        message.message_id,
        message.schema,
        message.mode,
        message.command_hash,
    )
    actual_header = (
        result.operation_id,
        result.message_id,
        result.schema,
        result.mode,
        result.command_hash,
    )
    if actual_header != expected_header:
        raise ValueError("result header does not match the persisted command")
    by_key = {item.idempotency_key: item for item in result.item_results}
    if len(by_key) != len(result.item_results):
        raise ValueError("result contains duplicate IdempotencyKey values")
    if set(by_key) != {row.idempotency_key for row in message.rows}:
        raise ValueError("result rows do not exactly match the persisted command")
    for row in message.rows:
        item = by_key[row.idempotency_key]
        _validate_item_identity(row, item)
        if item.result not in {"validated", "applied", "already_actual"}:
            continue
        old_categories = parse_category_guid_set(item.old_category_guids)
        projected_categories = parse_category_guid_set(item.projected_category_guids)
        readback_categories = parse_category_guid_set(item.readback_category_guids)
        expected_projected = set(old_categories)
        if row.category_mode in {"replace_expected", "remove_expected"}:
            expected_projected.discard(row.expected_category.guid)
        if row.category_mode != "remove_expected":
            expected_projected.add(row.target_category.guid)
        if projected_categories != frozenset(expected_projected):
            raise ValueError(f"projected categories changed for {row.idempotency_key}")
        if result.mode == "dry_run":
            if readback_categories != old_categories:
                raise ValueError(f"dry_run changed categories for {row.idempotency_key}")
        else:
            if readback_categories != projected_categories:
                raise ValueError(f"full category readback changed for {row.idempotency_key}")
            if _normalize_guid(item.readback_kind_guid) != row.target_kind.guid:
                raise ValueError(f"readback kind changed for {row.idempotency_key}")
            if _normalize_guid(item.readback_group_guid) != row.target_group.guid:
                raise ValueError(f"readback group changed for {row.idempotency_key}")


def parse_category_guid_set(value: str) -> frozenset[str]:
    if not value.strip():
        return frozenset()
    raw = [item.strip() for item in value.split(";") if item.strip()]
    normalized = [_normalize_guid(item) for item in raw]
    if len(normalized) != len(set(normalized)):
        raise ValueError("category GUID list contains duplicates")
    return frozenset(normalized)


def result_fingerprint(result: NomenclatureClassificationExchangeResult) -> str:
    # ProcessedAt is delivery metadata and legitimately changes when UT 10.3
    # repeats the same safe dry-run MessageId. Fingerprint the exact semantic
    # result so such a retry is idempotent, while every identity, outcome and
    # readback field remains protected against conflicting reuse.
    return _sha256_json(
        {
            "command_hash": result.command_hash,
            "errors": result.errors,
            "failed": result.failed,
            "items": [item.__dict__ for item in result.item_results],
            "loaded": result.loaded,
            "message_id": result.message_id,
            "mode": result.mode,
            "operation_id": result.operation_id,
            "schema": result.schema,
            "status": result.status,
        }
    )


def _validate_message(message: NomenclatureClassificationUpdateMessage) -> None:
    if message.schema != NOMENCLATURE_CLASSIFICATION_UPDATES_SCHEMA:
        raise ValueError(f"schema must be {NOMENCLATURE_CLASSIFICATION_UPDATES_SCHEMA}")
    if message.mode not in VALID_MODES:
        raise ValueError(f"mode must be one of: {', '.join(sorted(VALID_MODES))}")
    _clean_required(message.approved_by, "approved_by", max_length=150)
    operation_id = str(uuid.UUID(message.operation_id)).lower()
    if operation_id != message.operation_id:
        raise ValueError("operation_id must be a canonical lowercase UUID")
    if not _HEX_SHA256_RE.fullmatch(message.command_hash):
        raise ValueError("command_hash must be a lowercase SHA-256 hex value")
    if not _MESSAGE_ID_RE.fullmatch(message.message_id):
        raise ValueError("message_id contains unsupported characters")
    expected_message_id = build_nomenclature_classification_message_id(
        message.operation_id, message.command_hash, message.mode
    )
    if message.message_id != expected_message_id:
        raise ValueError("message_id does not match operation, hash and mode")
    intents = tuple(_intent_from_update(row) for row in message.rows)
    rows, command_hash, _ = prepare_nomenclature_classification_command(
        intents,
        approved_by=message.approved_by,
        source=message.source,
        target=message.target,
    )
    if rows != message.rows or command_hash != message.command_hash:
        raise ValueError("message hashes do not match canonical service payload")


def _validate_unique_rows(rows: tuple[NomenclatureClassificationIntentRow, ...]) -> None:
    keys = [row.idempotency_key for row in rows]
    if len(keys) != len(set(keys)):
        raise ValueError("message contains duplicate IdempotencyKey values")
    products = [row.nomenclature_guid for row in rows]
    if len(products) != len(set(products)):
        raise ValueError("message contains duplicate nomenclature products")


def _normalize_intent(
    row: NomenclatureClassificationIntentRow,
) -> NomenclatureClassificationIntentRow:
    group_mode = row.group_mode.strip().lower()
    if group_mode not in VALID_GROUP_MODES:
        raise ValueError(f"group_mode must be one of: {', '.join(sorted(VALID_GROUP_MODES))}")
    category_mode = row.category_mode.strip().lower()
    if category_mode not in VALID_CATEGORY_MODES:
        raise ValueError(f"category_mode must be one of: {', '.join(sorted(VALID_CATEGORY_MODES))}")
    expected_group = _normalize_reference(
        row.expected_group,
        "expected_group",
        allow_empty=group_mode == "set",
    )
    target_group = _normalize_reference(
        row.target_group,
        "target_group",
        allow_empty=group_mode == "clear_expected",
    )
    if group_mode == "clear_expected" and target_group != OneCClassificationReference():
        raise ValueError("target_group must be empty for clear_expected")
    expected_category = _normalize_reference(
        row.expected_category,
        "expected_category",
        allow_empty=category_mode == "ensure_present",
    )
    target_category = _normalize_reference(
        row.target_category,
        "target_category",
        allow_empty=category_mode == "remove_expected",
    )
    if category_mode == "remove_expected" and target_category != OneCClassificationReference():
        raise ValueError("target_category must be empty for remove_expected")
    return NomenclatureClassificationIntentRow(
        idempotency_key=_clean_required(row.idempotency_key, "idempotency_key", max_length=200),
        nomenclature_code=_clean_required(
            row.nomenclature_code, "nomenclature_code", max_length=64
        ),
        nomenclature_guid=_normalize_guid(row.nomenclature_guid),
        expected_kind=_normalize_reference(row.expected_kind, "expected_kind", allow_empty=True),
        target_kind=_normalize_reference(row.target_kind, "target_kind"),
        expected_group=expected_group,
        target_group=target_group,
        expected_category=expected_category,
        target_category=target_category,
        group_mode=group_mode,
        category_mode=category_mode,
        reason=_normalize_spaces(row.reason),
    )


def _normalize_reference(
    value: OneCClassificationReference,
    label: str,
    *,
    allow_empty: bool = False,
) -> OneCClassificationReference:
    guid = str(value.guid).strip()
    code = _normalize_spaces(value.code)
    name = _normalize_spaces(value.name)
    if not guid:
        if allow_empty and not code and not name:
            return OneCClassificationReference()
        raise ValueError(f"{label}.guid is required")
    return OneCClassificationReference(
        guid=_normalize_guid(guid),
        code=code,
        name=name,
    )


def _validate_item_identity(
    row: NomenclatureClassificationUpdateRow,
    item: NomenclatureClassificationItemResult,
) -> None:
    expected = (
        row.decision_hash,
        row.nomenclature_code,
        row.nomenclature_guid,
        row.expected_kind.guid,
        row.expected_kind.code,
        row.target_kind.guid,
        row.target_kind.code,
        row.expected_group.guid,
        row.expected_group.code,
        row.target_group.guid,
        row.target_group.code,
        row.group_mode,
        row.category_mode,
        row.expected_category.guid,
        row.expected_category.code,
        row.target_category.guid,
        row.target_category.code,
    )
    actual = (
        item.decision_hash,
        item.nomenclature_code,
        _normalize_guid(item.nomenclature_guid),
        _normalize_guid(item.expected_kind_guid),
        item.expected_kind_code,
        _normalize_guid(item.target_kind_guid),
        item.target_kind_code,
        _normalize_guid(item.expected_group_guid),
        item.expected_group_code,
        _normalize_guid(item.target_group_guid),
        item.target_group_code,
        item.group_mode,
        item.category_mode,
        _normalize_guid(item.expected_category_guid),
        item.expected_category_code,
        _normalize_guid(item.target_category_guid),
        item.target_category_code,
    )
    if actual != expected:
        raise ValueError(f"result identity changed for {row.idempotency_key}")


def _intent_payload(row: NomenclatureClassificationIntentRow) -> dict[str, object]:
    return {
        "category_mode": row.category_mode,
        "expected_category": row.expected_category.__dict__,
        "expected_group": row.expected_group.__dict__,
        "expected_kind": row.expected_kind.__dict__,
        "group_mode": row.group_mode,
        "idempotency_key": row.idempotency_key,
        "nomenclature_code": row.nomenclature_code,
        "nomenclature_guid": row.nomenclature_guid,
        "reason": row.reason,
        "target_category": row.target_category.__dict__,
        "target_group": row.target_group.__dict__,
        "target_kind": row.target_kind.__dict__,
    }


def _row_payload(row: NomenclatureClassificationUpdateRow) -> dict[str, object]:
    return {**_intent_payload(row), "decision_hash": row.decision_hash}


def _row_from_payload(item: dict[str, object]) -> NomenclatureClassificationUpdateRow:
    def reference(name: str) -> OneCClassificationReference:
        raw = item.get(name)
        if not isinstance(raw, dict):
            raise ValueError(f"canonical payload field {name} must be an object")
        return OneCClassificationReference(
            guid=str(raw.get("guid") or ""),
            code=str(raw.get("code") or ""),
            name=str(raw.get("name") or ""),
        )

    return NomenclatureClassificationUpdateRow(
        idempotency_key=str(item.get("idempotency_key") or ""),
        decision_hash=str(item.get("decision_hash") or ""),
        nomenclature_code=str(item.get("nomenclature_code") or ""),
        nomenclature_guid=str(item.get("nomenclature_guid") or ""),
        expected_kind=reference("expected_kind"),
        target_kind=reference("target_kind"),
        expected_group=reference("expected_group"),
        target_group=reference("target_group"),
        expected_category=reference("expected_category"),
        target_category=reference("target_category"),
        group_mode=str(item.get("group_mode") or ""),
        category_mode=str(item.get("category_mode") or ""),
        reason=str(item.get("reason") or ""),
    )


def _intent_from_update(
    row: NomenclatureClassificationUpdateRow,
) -> NomenclatureClassificationIntentRow:
    values = row.__dict__.copy()
    values.pop("decision_hash", None)
    return NomenclatureClassificationIntentRow(**values)


def _parse_item_result(node: ET.Element) -> NomenclatureClassificationItemResult:
    def present(name: str) -> str:
        return _present_node_text(node, name)

    return NomenclatureClassificationItemResult(
        idempotency_key=present("IdempotencyKey"),
        decision_hash=present("DecisionHash").lower(),
        nomenclature_code=present("NomenclatureCode"),
        nomenclature_guid=present("NomenclatureGuid"),
        expected_kind_guid=present("ExpectedKindGuid"),
        expected_kind_code=present("ExpectedKindCode"),
        target_kind_guid=present("TargetKindGuid"),
        target_kind_code=present("TargetKindCode"),
        expected_group_guid=present("ExpectedGroupGuid"),
        expected_group_code=present("ExpectedGroupCode"),
        target_group_guid=present("TargetGroupGuid"),
        target_group_code=present("TargetGroupCode"),
        group_mode=present("GroupMode"),
        category_mode=present("CategoryMode"),
        expected_category_guid=present("ExpectedCategoryGuid"),
        expected_category_code=present("ExpectedCategoryCode"),
        target_category_guid=present("TargetCategoryGuid"),
        target_category_code=present("TargetCategoryCode"),
        result=present("Result"),
        message=present("Message"),
        old_kind_guid=present("OldKindGuid"),
        readback_kind_guid=present("ReadbackKindGuid"),
        old_group_guid=present("OldGroupGuid"),
        readback_group_guid=present("ReadbackGroupGuid"),
        old_category_guids=present("OldCategoryGuids"),
        projected_category_guids=present("ProjectedCategoryGuids"),
        readback_category_guids=present("ReadbackCategoryGuids"),
    )


def _add_reference(parent: ET.Element, name: str, value: OneCClassificationReference) -> None:
    _add_text(parent, f"{name}Guid", value.guid)
    _add_text(parent, f"{name}Code", value.code)
    _add_text(parent, f"{name}Name", value.name)


def _add_text(parent: ET.Element, name: str, value: object) -> None:
    child = ET.SubElement(parent, name)
    child.text = str(value)


def _format_created_at(value: datetime | None) -> str:
    created_at = value or datetime.now().astimezone()
    return created_at.isoformat(timespec="seconds")


def _normalize_guid(value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    try:
        return str(uuid.UUID(text)).lower()
    except ValueError as error:
        raise ValueError(f"invalid GUID: {value}") from error


def _clean_required(value: str, label: str, *, max_length: int) -> str:
    normalized = _normalize_spaces(value)
    if not normalized:
        raise ValueError(f"{label} is required")
    if len(normalized) > max_length:
        raise ValueError(f"{label} cannot exceed {max_length} characters")
    return normalized


def _normalize_spaces(value: object) -> str:
    return " ".join(str(value).split())


def _sha256_json(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _node_text(root: ET.Element, tag: str) -> str:
    node = root.find(tag)
    if node is None or node.text is None:
        return ""
    return node.text.strip()


def _present_node_text(root: ET.Element, tag: str) -> str:
    node = root.find(tag)
    if node is None:
        raise ValueError(f"result is missing required field: {tag}")
    return (node.text or "").strip()


def _required_node_text(root: ET.Element, tag: str) -> str:
    value = _present_node_text(root, tag)
    if not value:
        raise ValueError(f"result field is empty: {tag}")
    return value


def _parse_int(value: str, field_name: str) -> int:
    normalized = value.replace(" ", "").replace("\xa0", "").replace("\u202f", "")
    try:
        return int(normalized)
    except ValueError as error:
        raise ValueError(f"{field_name} must be integer, got: {value}") from error
