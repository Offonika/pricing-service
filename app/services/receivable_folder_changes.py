from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import ReceivableFolderChangeOperation
from app.services.exporters.ut103_customer_price_types import one_c_guid_from_counterparty_ref

COMMAND_TYPE = "move_counterparty_folder"
ACTIVE_STATES = {"draft", "dry_run_sent", "dry_run_ok", "apply_sent"}


def create_folder_change_operation(
    db: Session,
    *,
    signal: dict[str, Any],
    data_version: str,
) -> ReceivableFolderChangeOperation:
    if signal.get("queue") != "actionable" or not signal.get("action_required"):
        raise ValueError("folder change can only be prepared for an actionable signal")
    required = {
        "signal_key": signal.get("signal_key"),
        "counterparty_ref": signal.get("counterparty_ref"),
        "current_folder_ref": signal.get("current_folder_ref"),
        "recommended_folder_ref": signal.get("recommended_folder_ref"),
    }
    missing = [key for key, value in required.items() if not str(value or "").strip()]
    if missing:
        raise ValueError("folder signal is incomplete: " + ", ".join(missing))
    counterparty_key = str(signal["counterparty_ref"]).strip().casefold()
    current = db.scalar(
        select(ReceivableFolderChangeOperation).where(
            ReceivableFolderChangeOperation.active_counterparty_key == counterparty_key
        )
    )
    if current is not None:
        return current
    operation = ReceivableFolderChangeOperation(
        signal_key=str(signal["signal_key"]),
        counterparty_ref=str(signal["counterparty_ref"]),
        counterparty_code=_optional_text(signal.get("counterparty_code")),
        counterparty_name=_optional_text(signal.get("counterparty_name")),
        active_counterparty_key=counterparty_key,
        expected_old_folder_ref=str(signal["current_folder_ref"]),
        expected_old_folder_name=_optional_text(signal.get("current_folder_name")),
        proposed_new_folder_ref=str(signal["recommended_folder_ref"]),
        proposed_new_folder_name=_optional_text(signal.get("recommended_folder_name")),
        signal_snapshot=_json_safe(signal),
        data_version=str(data_version),
        state="draft",
    )
    db.add(operation)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        current = db.scalar(
            select(ReceivableFolderChangeOperation).where(
                ReceivableFolderChangeOperation.active_counterparty_key == counterparty_key
            )
        )
        if current is not None:
            return current
        raise
    db.refresh(operation)
    return operation


def publish_folder_change_dry_run(
    db: Session,
    operation: ReceivableFolderChangeOperation,
    *,
    exchange_root: str | Path,
) -> ReceivableFolderChangeOperation:
    if operation.state != "draft":
        return operation
    operation.dry_run_message_id = _message_id(operation, "dry-run")
    operation.state = "dry_run_sent"
    db.commit()
    try:
        _write_message(exchange_root, operation, mode="dry_run")
    except Exception as error:
        operation.state = "failed"
        operation.active_counterparty_key = None
        operation.last_error = str(error)[:2000]
        db.commit()
        raise
    return operation


def approve_folder_change_operation(
    db: Session,
    operation: ReceivableFolderChangeOperation,
    *,
    approved_by_bitrix_user_id: str,
    exchange_root: str | Path,
) -> ReceivableFolderChangeOperation:
    if operation.state != "dry_run_ok":
        raise ValueError("apply requires a successful dry-run")
    if not str(approved_by_bitrix_user_id).strip():
        raise ValueError("Bitrix approver is required")
    approved_at = datetime.now(timezone.utc).replace(tzinfo=None)
    operation.approved_by_bitrix_user_id = str(approved_by_bitrix_user_id).strip()
    operation.approved_at = approved_at
    operation.decision_hash = _decision_hash(operation)
    operation.apply_message_id = _message_id(operation, "apply")
    operation.state = "apply_sent"
    db.commit()
    try:
        _write_message(exchange_root, operation, mode="apply")
    except Exception as error:
        # После неизвестного результата файловой публикации повторный apply запрещён.
        operation.state = "needs_review"
        operation.last_error = f"apply publish is ambiguous: {error}"[:2000]
        db.commit()
        raise
    return operation


def sync_folder_change_results(db: Session, *, exchange_root: str | Path) -> dict[str, int]:
    operations = (
        db.execute(
            select(ReceivableFolderChangeOperation).where(
                ReceivableFolderChangeOperation.state.in_(("dry_run_sent", "apply_sent"))
            )
        )
        .scalars()
        .all()
    )
    summary = {"checked": len(operations), "updated": 0, "failed": 0}
    for operation in operations:
        message_id = (
            operation.dry_run_message_id
            if operation.state == "dry_run_sent"
            else operation.apply_message_id
        )
        path = _result_path(exchange_root, str(message_id or ""))
        if not path.exists():
            continue
        try:
            result = _parse_result(path)
            _apply_result(operation, result)
            summary["updated"] += 1
        except Exception as error:
            operation.state = "needs_review"
            operation.last_error = str(error)[:2000]
            summary["failed"] += 1
        db.commit()
        _archive_result(path)
    return summary


def operation_payload(operation: ReceivableFolderChangeOperation) -> dict[str, Any]:
    return {
        "id": operation.id,
        "signal_key": operation.signal_key,
        "counterparty_ref": operation.counterparty_ref,
        "counterparty_code": operation.counterparty_code,
        "counterparty_name": operation.counterparty_name,
        "expected_old_folder_ref": operation.expected_old_folder_ref,
        "expected_old_folder_name": operation.expected_old_folder_name,
        "proposed_new_folder_ref": operation.proposed_new_folder_ref,
        "proposed_new_folder_name": operation.proposed_new_folder_name,
        "data_version": operation.data_version,
        "decision_hash": operation.decision_hash,
        "approved_by_bitrix_user_id": operation.approved_by_bitrix_user_id,
        "state": operation.state,
        "readback_folder_ref": operation.readback_folder_ref,
        "readback_folder_name": operation.readback_folder_name,
        "last_error": operation.last_error,
        "created_at": operation.created_at,
        "updated_at": operation.updated_at,
    }


def _apply_result(operation: ReceivableFolderChangeOperation, result: dict[str, str]) -> None:
    expected_message_id = (
        operation.dry_run_message_id
        if operation.state == "dry_run_sent"
        else operation.apply_message_id
    )
    if result.get("message_id") != expected_message_id:
        raise ValueError("folder result MessageId mismatch")
    if result.get("counterparty_ref") != operation.counterparty_ref:
        raise ValueError("folder result CounterpartyRef mismatch")
    if result.get("expected_old_folder_ref") != operation.expected_old_folder_ref:
        raise ValueError("folder result old folder mismatch")
    if result.get("proposed_new_folder_ref") != operation.proposed_new_folder_ref:
        raise ValueError("folder result proposed folder mismatch")
    status = result.get("status")
    if operation.state == "dry_run_sent" and status == "validated":
        operation.state = "dry_run_ok"
        operation.last_error = None
        return
    if operation.state == "apply_sent" and status in {"applied", "already_actual"}:
        if result.get("readback_folder_ref") != operation.proposed_new_folder_ref:
            raise ValueError("folder apply readback mismatch")
        if result.get("decision_hash") != operation.decision_hash:
            raise ValueError("folder apply DecisionHash mismatch")
        operation.state = "applied"
        operation.active_counterparty_key = None
        operation.readback_folder_ref = result.get("readback_folder_ref")
        operation.readback_folder_name = result.get("readback_folder_name")
        operation.last_error = None
        return
    operation.state = "needs_review" if status == "needs_review" else "failed"
    operation.active_counterparty_key = None
    operation.last_error = result.get("message") or f"1C status: {status}"


def _write_message(
    exchange_root: str | Path,
    operation: ReceivableFolderChangeOperation,
    *,
    mode: str,
) -> Path:
    message_id = operation.dry_run_message_id if mode == "dry_run" else operation.apply_message_id
    root = ET.Element("ExchangeMessage")
    header = ET.SubElement(root, "Header")
    values = {
        "MessageId": message_id,
        "Schema": "onec_commands.v1",
        "CreatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "Source": "pricing-service",
        "Target": "1c_ut_10_3",
        "Mode": mode,
    }
    for key, value in values.items():
        ET.SubElement(header, key).text = str(value)
    command = ET.SubElement(ET.SubElement(root, "Commands"), "Command")
    command_values = {
        "CommandType": COMMAND_TYPE,
        "IdempotencyKey": f"receivable-folder-change:{operation.id}:{operation.data_version}",
        "OperationId": operation.id,
        "SignalKey": operation.signal_key,
        "DecisionHash": operation.decision_hash or "",
        "DataVersion": operation.data_version,
        "CounterpartyRef": operation.counterparty_ref,
        "CounterpartyGuid": one_c_guid_from_counterparty_ref(operation.counterparty_ref),
        "CounterpartyCode": operation.counterparty_code or "",
        "ExpectedOldFolderRef": operation.expected_old_folder_ref,
        "ExpectedOldFolderGuid": one_c_guid_from_counterparty_ref(
            operation.expected_old_folder_ref
        ),
        "ProposedNewFolderRef": operation.proposed_new_folder_ref,
        "ProposedNewFolderGuid": one_c_guid_from_counterparty_ref(
            operation.proposed_new_folder_ref
        ),
        "ApprovedBy": operation.approved_by_bitrix_user_id or "",
        "ApprovedAt": operation.approved_at.isoformat() if operation.approved_at else "",
    }
    for key, value in command_values.items():
        ET.SubElement(command, key).text = str(value)
    ET.indent(root, space="  ")
    payload = ET.tostring(root, encoding="windows-1251", xml_declaration=True)
    output_dir = Path(exchange_root) / "to_1c" / "new"
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"onec_commands_{message_id}.ready.xml"
    temporary = output_dir / f"{target.name}.{uuid.uuid4().hex}.tmp"
    temporary.write_bytes(payload)
    os.replace(temporary, target)
    return target


def _parse_result(path: Path) -> dict[str, str]:
    root = ET.parse(path).getroot()
    command = root.find("CommandResults/CommandResult")
    if root.findtext("Schema") != "onec_commands.v1" or command is None:
        raise ValueError("invalid folder result schema")
    return {
        "message_id": str(root.findtext("MessageId") or ""),
        "status": str(command.findtext("Status") or ""),
        "message": str(command.findtext("Message") or ""),
        "decision_hash": str(command.findtext("DecisionHash") or ""),
        "counterparty_ref": str(command.findtext("CounterpartyRef") or ""),
        "expected_old_folder_ref": str(command.findtext("ExpectedOldFolderRef") or ""),
        "proposed_new_folder_ref": str(command.findtext("ProposedNewFolderRef") or ""),
        "readback_folder_ref": str(command.findtext("ReadbackFolderRef") or ""),
        "readback_folder_name": str(command.findtext("ReadbackFolderName") or ""),
    }


def _message_id(operation: ReceivableFolderChangeOperation, suffix: str) -> str:
    identity = hashlib.sha256(
        f"{operation.signal_key}|{operation.data_version}".encode()
    ).hexdigest()[:12]
    return f"rfc-{operation.id}-{identity}-{suffix}"


def _decision_hash(operation: ReceivableFolderChangeOperation) -> str:
    payload = {
        "operation_id": operation.id,
        "signal_key": operation.signal_key,
        "data_version": operation.data_version,
        "counterparty_ref": operation.counterparty_ref,
        "expected_old_folder_ref": operation.expected_old_folder_ref,
        "proposed_new_folder_ref": operation.proposed_new_folder_ref,
        "approved_by": operation.approved_by_bitrix_user_id,
        "approved_at": operation.approved_at.isoformat() if operation.approved_at else None,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _result_path(exchange_root: str | Path, message_id: str) -> Path:
    return Path(exchange_root) / "from_1c" / "new" / f"onec_commands_{message_id}.result.xml"


def _archive_result(path: Path) -> None:
    archive = path.parent.parent / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    os.replace(path, archive / path.name)


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))
