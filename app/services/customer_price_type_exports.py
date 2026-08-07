"""Fail-closed selection of approved customer price-type cases for UT 10.3."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.customer_price_type import (
    CustomerPriceTypeCase,
    CustomerPriceTypeCaseEvent,
    CustomerPriceTypeProfile,
    CustomerPriceTypeSnapshot,
)
from app.services.exporters.ut103_customer_price_types import (
    CUSTOMER_PRICE_TYPE_UPDATES_SCHEMA,
    EXPECTED_CURRENT_PRICE_TYPE,
    TARGET_PRICE_TYPE,
    CustomerPriceTypeExchangeResult,
    CustomerPriceTypeItemResult,
    CustomerPriceTypeUpdateMessage,
    CustomerPriceTypeUpdateRow,
    one_c_guid_from_counterparty_ref,
)

READY_STAGE = "READY_FOR_1C"
READY_EXPORT_STATUS = "ready"
APPROVED_STATUS = "approved"
APPROVED_FINAL_DECISION = "downgrade_to_retail"
EXPECTED_RECOMMENDATION = "downgrade_to_retail"
ALLOWED_APPROVED_STOP_FACTORS = frozenset({"human_approval_required"})
EXPORT_REQUESTED_EVENT = "onec_export_requested"
EXPORT_QUEUED_EVENT = "onec_export_queued"
DRY_RUN_SUCCEEDED_EVENT = "onec_dry_run_succeeded"
DRY_RUN_FAILED_EVENT = "onec_dry_run_failed"
APPLY_SUCCEEDED_EVENT = "onec_apply_succeeded"
APPLY_FAILED_EVENT = "onec_apply_failed"


class CustomerPriceTypeExportGateError(ValueError):
    """Raised when a case marked ready is not safe to export."""


@dataclass(frozen=True, slots=True)
class ApprovedCustomerPriceTypeExportSelection:
    snapshot_month: date
    case_ids: tuple[int, ...]
    approved_by: str
    rows: tuple[CustomerPriceTypeUpdateRow, ...]


@dataclass(frozen=True, slots=True)
class RecordedCustomerPriceTypeExchangeResult:
    message_id: str
    mode: str
    case_ids: tuple[int, ...]
    succeeded: bool
    idempotent_replay: bool


def select_approved_customer_price_type_updates(
    session: Session,
    *,
    snapshot_month: date,
    case_ids: tuple[int, ...] = (),
) -> ApprovedCustomerPriceTypeExportSelection:
    """Return only current, approved and contract-supported READY_FOR_1C cases."""

    statement = (
        select(CustomerPriceTypeCase, CustomerPriceTypeProfile, CustomerPriceTypeSnapshot)
        .join(
            CustomerPriceTypeProfile,
            CustomerPriceTypeProfile.id == CustomerPriceTypeCase.profile_id,
        )
        .join(
            CustomerPriceTypeSnapshot,
            CustomerPriceTypeSnapshot.id == CustomerPriceTypeCase.current_snapshot_id,
        )
        .where(
            CustomerPriceTypeCase.snapshot_month == snapshot_month,
            CustomerPriceTypeCase.stage == READY_STAGE,
            CustomerPriceTypeCase.onec_export_status == READY_EXPORT_STATUS,
        )
        .order_by(CustomerPriceTypeCase.id.asc())
    )
    requested_ids = tuple(dict.fromkeys(case_ids))
    if requested_ids:
        statement = statement.where(CustomerPriceTypeCase.id.in_(requested_ids))

    selected = list(session.execute(statement).all())
    if requested_ids:
        found_ids = {case.id for case, _, _ in selected}
        missing_ids = [case_id for case_id in requested_ids if case_id not in found_ids]
        if missing_ids:
            raise CustomerPriceTypeExportGateError(
                "requested cases are not READY_FOR_1C with onec_export_status=ready: "
                + ", ".join(str(case_id) for case_id in missing_ids)
            )
    if not selected:
        raise CustomerPriceTypeExportGateError(
            f"no approved READY_FOR_1C cases found for {snapshot_month:%Y-%m}"
        )

    export_rows: list[CustomerPriceTypeUpdateRow] = []
    selected_case_ids: list[int] = []
    approver_names: set[str] = set()
    gate_errors: list[str] = []
    for case, profile, snapshot in selected:
        errors = _case_gate_errors(case=case, profile=profile, snapshot=snapshot)
        if errors:
            gate_errors.append(f"case {case.id} ({case.case_key}): " + "; ".join(errors))
            continue
        counterparty_ref = profile.counterparty_ref.strip().upper()
        export_rows.append(
            CustomerPriceTypeUpdateRow(
                idempotency_key=(f"customer-price-type:{case.case_key}:{snapshot.snapshot_hash}"),
                counterparty_ref=counterparty_ref,
                counterparty_guid=one_c_guid_from_counterparty_ref(counterparty_ref),
                counterparty_name=str(profile.counterparty_name).strip(),
                expected_current_price_type=EXPECTED_CURRENT_PRICE_TYPE,
                target_price_type=TARGET_PRICE_TYPE,
                reason=(
                    f"case={case.case_key}; snapshot={snapshot.snapshot_hash[:12]}; "
                    f"approved_by={case.approver_name}"
                ),
            )
        )
        selected_case_ids.append(case.id)
        approver_names.add(case.approver_name.strip())

    if gate_errors:
        raise CustomerPriceTypeExportGateError(
            "export gate rejected ready cases: " + " | ".join(gate_errors)
        )
    if len(approver_names) != 1:
        raise CustomerPriceTypeExportGateError(
            "all cases in one package must have the same approved_by; split the batch"
        )

    return ApprovedCustomerPriceTypeExportSelection(
        snapshot_month=snapshot_month,
        case_ids=tuple(selected_case_ids),
        approved_by=next(iter(approver_names)),
        rows=tuple(export_rows),
    )


def record_customer_price_type_export_request(
    session: Session,
    *,
    selection: ApprovedCustomerPriceTypeExportSelection,
    message: CustomerPriceTypeUpdateMessage,
) -> bool:
    """Append an idempotent request event before a DB-backed package is queued."""

    _validate_message_selection(selection, message)
    batch_fingerprint = _batch_fingerprint(selection, message)
    event_key = _event_key("request", message.mode, message.message_id)
    existing = _events_by_key(session, event_key)
    if existing:
        _assert_existing_batch(
            existing,
            case_ids=selection.case_ids,
            batch_fingerprint=batch_fingerprint,
        )
        return True

    cases = _cases_by_id(session, selection.case_ids)
    for case_id, row in zip(selection.case_ids, message.rows, strict=True):
        case = cases[case_id]
        session.add(
            CustomerPriceTypeCaseEvent(
                case_id=case_id,
                event_type=EXPORT_REQUESTED_EVENT,
                actor="pricing-service",
                source="system",
                before_status=case.onec_export_status,
                after_status=case.onec_export_status,
                comment=f"{message.mode} package requested: {message.message_id}",
                metadata_json=_request_metadata(
                    case=case,
                    row=row,
                    message=message,
                    batch_fingerprint=batch_fingerprint,
                ),
                idempotency_key=event_key,
            )
        )
    session.flush()
    return False


def record_customer_price_type_export_queued(
    session: Session,
    *,
    selection: ApprovedCustomerPriceTypeExportSelection,
    message: CustomerPriceTypeUpdateMessage,
    path: str,
) -> bool:
    """Append queue events and move apply cases to pending readback."""

    _validate_message_selection(selection, message)
    batch_fingerprint = _batch_fingerprint(selection, message)
    request_events = _events_by_key(
        session, _event_key("request", message.mode, message.message_id)
    )
    _assert_existing_batch(
        request_events,
        case_ids=selection.case_ids,
        batch_fingerprint=batch_fingerprint,
    )

    event_key = _event_key("queued", message.mode, message.message_id)
    existing = _events_by_key(session, event_key)
    if existing:
        _assert_existing_batch(
            existing,
            case_ids=selection.case_ids,
            batch_fingerprint=batch_fingerprint,
        )
        return True

    cases = _cases_by_id(session, selection.case_ids)
    for case_id, row in zip(selection.case_ids, message.rows, strict=True):
        case = cases[case_id]
        before_status = case.onec_export_status
        if message.mode == "apply":
            if (
                case.onec_export_status != READY_EXPORT_STATUS
                or case.onec_readback_status != "not_requested"
            ):
                raise CustomerPriceTypeExportGateError(
                    f"case {case_id} changed before apply was queued"
                )
            case.onec_export_status = "exported"
            case.onec_readback_status = "pending"
            case.version += 1
        metadata = _request_metadata(
            case=case,
            row=row,
            message=message,
            batch_fingerprint=batch_fingerprint,
        )
        metadata["path"] = path
        session.add(
            CustomerPriceTypeCaseEvent(
                case_id=case_id,
                event_type=EXPORT_QUEUED_EVENT,
                actor="pricing-service",
                source="system",
                before_status=before_status,
                after_status=case.onec_export_status,
                comment=f"{message.mode} package queued: {message.message_id}",
                metadata_json=metadata,
                idempotency_key=event_key,
            )
        )
    session.flush()
    return False


def require_successful_customer_price_type_dry_run(
    session: Session,
    *,
    selection: ApprovedCustomerPriceTypeExportSelection,
    dry_run_message_id: str,
) -> None:
    """Require one persisted successful dry-run for every selected case."""

    events = _events_by_key(session, _event_key("result", "dry_run", dry_run_message_id))
    by_case = {
        event.case_id: event for event in events if event.event_type == DRY_RUN_SUCCEEDED_EVENT
    }
    missing: list[int] = []
    for case_id, row in zip(selection.case_ids, selection.rows, strict=True):
        event = by_case.get(case_id)
        metadata = event.metadata_json if event is not None else {}
        if (
            event is None
            or metadata.get("snapshot_hash") != row.idempotency_key.rsplit(":", maxsplit=1)[-1]
            or metadata.get("row_idempotency_key") != row.idempotency_key
            or metadata.get("approved_by") != selection.approved_by
            or metadata.get("result") != "validated"
            or metadata.get("current_price_type") != EXPECTED_CURRENT_PRICE_TYPE
            or metadata.get("readback_price_type") != EXPECTED_CURRENT_PRICE_TYPE
            or metadata.get("target_price_type") != TARGET_PRICE_TYPE
        ):
            missing.append(case_id)
    if missing:
        raise CustomerPriceTypeExportGateError(
            "apply requires the specified persisted successful dry_run for cases: "
            + ", ".join(str(case_id) for case_id in missing)
        )


def record_customer_price_type_exchange_result(
    session: Session,
    result: CustomerPriceTypeExchangeResult,
) -> RecordedCustomerPriceTypeExchangeResult:
    """Persist one trusted 1C result and update apply/readback state atomically."""

    if result.schema != CUSTOMER_PRICE_TYPE_UPDATES_SCHEMA:
        raise CustomerPriceTypeExportGateError(
            f"result schema must be {CUSTOMER_PRICE_TYPE_UPDATES_SCHEMA}"
        )
    if result.status not in {"success", "partial", "failed"}:
        raise CustomerPriceTypeExportGateError("unsupported result status")

    request_matches: list[tuple[str, list[CustomerPriceTypeCaseEvent]]] = []
    for mode in ("dry_run", "apply"):
        events = _events_by_key(session, _event_key("request", mode, result.message_id))
        if events:
            request_matches.append((mode, events))
    if len(request_matches) != 1:
        raise CustomerPriceTypeExportGateError(
            "result must match exactly one persisted export request"
        )
    mode, request_events = request_matches[0]
    queued_events = _events_by_key(session, _event_key("queued", mode, result.message_id))
    request_case_ids = tuple(sorted(event.case_id for event in request_events))
    batch_fingerprint = _common_metadata_value(request_events, "batch_fingerprint")
    _assert_existing_batch(
        queued_events,
        case_ids=request_case_ids,
        batch_fingerprint=batch_fingerprint,
    )

    request_by_row = {
        str(event.metadata_json.get("row_idempotency_key") or ""): event for event in request_events
    }
    if "" in request_by_row or len(request_by_row) != len(request_events):
        raise CustomerPriceTypeExportGateError("persisted request row identity is invalid")
    result_by_row: dict[str, CustomerPriceTypeItemResult] = {}
    for item in result.item_results:
        if item.idempotency_key in result_by_row:
            raise CustomerPriceTypeExportGateError(
                f"duplicate result idempotency_key: {item.idempotency_key}"
            )
        if item.idempotency_key not in request_by_row:
            raise CustomerPriceTypeExportGateError(
                f"unexpected result idempotency_key: {item.idempotency_key}"
            )
        _validate_result_item_identity(item, request_by_row[item.idempotency_key].metadata_json)
        result_by_row[item.idempotency_key] = item

    cases = _cases_by_id(session, request_case_ids)
    for event in request_events:
        metadata = event.metadata_json
        case = cases[event.case_id]
        if case.case_key != metadata.get("case_key") or case.approved_snapshot_hash != metadata.get(
            "snapshot_hash"
        ):
            raise CustomerPriceTypeExportGateError(
                f"result belongs to stale case identity: {case.id}"
            )

    succeeded = _result_is_successful(
        mode=mode,
        result=result,
        expected_rows=request_by_row,
        result_by_row=result_by_row,
    )
    result_fingerprint = _result_fingerprint(result)
    result_event_key = _event_key("result", mode, result.message_id)
    existing = _events_by_key(session, result_event_key)
    if existing:
        _assert_existing_result(
            existing,
            case_ids=request_case_ids,
            result_fingerprint=result_fingerprint,
        )
        return RecordedCustomerPriceTypeExchangeResult(
            message_id=result.message_id,
            mode=mode,
            case_ids=request_case_ids,
            succeeded=existing[0].event_type in {DRY_RUN_SUCCEEDED_EVENT, APPLY_SUCCEEDED_EVENT},
            idempotent_replay=True,
        )

    event_type = {
        ("dry_run", True): DRY_RUN_SUCCEEDED_EVENT,
        ("dry_run", False): DRY_RUN_FAILED_EVENT,
        ("apply", True): APPLY_SUCCEEDED_EVENT,
        ("apply", False): APPLY_FAILED_EVENT,
    }[(mode, succeeded)]
    for request_event in request_events:
        case = cases[request_event.case_id]
        request_metadata = request_event.metadata_json
        item = result_by_row.get(str(request_metadata["row_idempotency_key"]))
        before_stage = case.stage
        if mode == "apply":
            if succeeded:
                case.stage = "CLOSED_CHANGED"
                case.onec_export_status = "exported"
                case.onec_readback_status = "confirmed"
            else:
                case.stage = "ONEC_ERROR"
                case.onec_export_status = "error"
                case.onec_readback_status = "error"
            case.version += 1
        metadata = {
            **request_metadata,
            "result_fingerprint": result_fingerprint,
            "result_status": result.status,
            "processed_at": result.processed_at,
            "loaded": result.loaded,
            "failed": result.failed,
            "errors": result.errors,
            "result_path": str(result.path) if result.path else "",
            "result_sha256": _result_file_sha256(result),
            **_item_result_metadata(item),
        }
        session.add(
            CustomerPriceTypeCaseEvent(
                case_id=case.id,
                event_type=event_type,
                actor="onec",
                source="onec",
                before_status=before_stage,
                after_status=case.stage,
                comment=f"{mode} result {result.status}: {result.message_id}",
                metadata_json=metadata,
                idempotency_key=result_event_key,
            )
        )
    session.flush()
    return RecordedCustomerPriceTypeExchangeResult(
        message_id=result.message_id,
        mode=mode,
        case_ids=request_case_ids,
        succeeded=succeeded,
        idempotent_replay=False,
    )


def _validate_message_selection(
    selection: ApprovedCustomerPriceTypeExportSelection,
    message: CustomerPriceTypeUpdateMessage,
) -> None:
    if len(selection.case_ids) != len(message.rows) or selection.rows != message.rows:
        raise CustomerPriceTypeExportGateError("message rows do not match selected cases")
    if selection.approved_by != message.approved_by:
        raise CustomerPriceTypeExportGateError("message ApprovedBy does not match selected cases")


def _batch_fingerprint(
    selection: ApprovedCustomerPriceTypeExportSelection,
    message: CustomerPriceTypeUpdateMessage,
) -> str:
    payload = {
        "message_id": message.message_id,
        "mode": message.mode,
        "approved_by": message.approved_by,
        "source": message.source,
        "case_rows": [
            {
                "case_id": case_id,
                "idempotency_key": row.idempotency_key,
                "counterparty_ref": row.counterparty_ref,
                "expected_current_price_type": row.expected_current_price_type,
                "target_price_type": row.target_price_type,
            }
            for case_id, row in zip(selection.case_ids, message.rows, strict=True)
        ],
    }
    return _sha256_json(payload)


def _request_metadata(
    *,
    case: CustomerPriceTypeCase,
    row: CustomerPriceTypeUpdateRow,
    message: CustomerPriceTypeUpdateMessage,
    batch_fingerprint: str,
) -> dict[str, Any]:
    return {
        "message_id": message.message_id,
        "mode": message.mode,
        "batch_fingerprint": batch_fingerprint,
        "case_key": case.case_key,
        "snapshot_hash": case.approved_snapshot_hash,
        "row_idempotency_key": row.idempotency_key,
        "counterparty_ref": row.counterparty_ref,
        "counterparty_guid": row.counterparty_guid,
        "expected_current_price_type": row.expected_current_price_type,
        "target_price_type": row.target_price_type,
        "approved_by": message.approved_by,
    }


def _validate_result_item_identity(
    item: CustomerPriceTypeItemResult,
    request_metadata: dict[str, Any],
) -> None:
    if (
        item.counterparty_ref.strip().upper()
        != str(request_metadata.get("counterparty_ref") or "").upper()
    ):
        raise CustomerPriceTypeExportGateError("result counterparty_ref identity mismatch")
    if (
        item.counterparty_guid.strip().lower()
        != str(request_metadata.get("counterparty_guid") or "").lower()
    ):
        raise CustomerPriceTypeExportGateError("result counterparty_guid identity mismatch")
    if item.target_price_type != request_metadata.get("target_price_type"):
        raise CustomerPriceTypeExportGateError("result target_price_type identity mismatch")


def _result_is_successful(
    *,
    mode: str,
    result: CustomerPriceTypeExchangeResult,
    expected_rows: dict[str, CustomerPriceTypeCaseEvent],
    result_by_row: dict[str, CustomerPriceTypeItemResult],
) -> bool:
    if (
        result.status != "success"
        or result.failed != 0
        or result.loaded != len(expected_rows)
        or set(result_by_row) != set(expected_rows)
    ):
        return False
    if mode == "dry_run":
        return all(
            item.result == "validated"
            and item.current_price_type == EXPECTED_CURRENT_PRICE_TYPE
            and item.readback_price_type == EXPECTED_CURRENT_PRICE_TYPE
            and item.target_price_type == TARGET_PRICE_TYPE
            for item in result_by_row.values()
        )
    return all(
        item.result in {"applied", "already_actual"}
        and item.current_price_type in {EXPECTED_CURRENT_PRICE_TYPE, TARGET_PRICE_TYPE}
        and item.target_price_type == TARGET_PRICE_TYPE
        and item.readback_price_type == TARGET_PRICE_TYPE
        for item in result_by_row.values()
    )


def _item_result_metadata(item: CustomerPriceTypeItemResult | None) -> dict[str, Any]:
    if item is None:
        return {"result": "missing"}
    return {
        "result": item.result,
        "message": item.message,
        "contract_guid": item.contract_guid,
        "contract_name": item.contract_name,
        "current_price_type": item.current_price_type,
        "target_price_type": item.target_price_type,
        "readback_price_type": item.readback_price_type,
        "found_contracts": item.found_contracts,
    }


def _result_fingerprint(result: CustomerPriceTypeExchangeResult) -> str:
    return _sha256_json(
        {
            "message_id": result.message_id,
            "schema": result.schema,
            "status": result.status,
            "processed_at": result.processed_at,
            "loaded": result.loaded,
            "failed": result.failed,
            "errors": result.errors,
            "items": [
                {
                    "idempotency_key": item.idempotency_key,
                    "counterparty_ref": item.counterparty_ref,
                    "counterparty_guid": item.counterparty_guid,
                    **_item_result_metadata(item),
                }
                for item in result.item_results
            ],
        }
    )


def _result_file_sha256(result: CustomerPriceTypeExchangeResult) -> str:
    if result.path is None or not result.path.is_file():
        return ""
    return hashlib.sha256(result.path.read_bytes()).hexdigest()


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _event_key(kind: str, mode: str, message_id: str) -> str:
    digest = hashlib.sha256(message_id.encode("utf-8")).hexdigest()
    return f"customer-price-type-onec:{kind}:{mode}:{digest}"


def _events_by_key(session: Session, idempotency_key: str) -> list[CustomerPriceTypeCaseEvent]:
    return list(
        session.scalars(
            select(CustomerPriceTypeCaseEvent)
            .where(CustomerPriceTypeCaseEvent.idempotency_key == idempotency_key)
            .order_by(CustomerPriceTypeCaseEvent.case_id.asc())
        ).all()
    )


def _cases_by_id(
    session: Session,
    case_ids: tuple[int, ...],
) -> dict[int, CustomerPriceTypeCase]:
    cases = {
        case.id: case
        for case in session.scalars(
            select(CustomerPriceTypeCase).where(CustomerPriceTypeCase.id.in_(case_ids))
        ).all()
    }
    missing = [case_id for case_id in case_ids if case_id not in cases]
    if missing:
        raise CustomerPriceTypeExportGateError(
            "customer price-type cases disappeared: "
            + ", ".join(str(case_id) for case_id in missing)
        )
    return cases


def _assert_existing_batch(
    events: list[CustomerPriceTypeCaseEvent],
    *,
    case_ids: tuple[int, ...],
    batch_fingerprint: str,
) -> None:
    if {event.case_id for event in events} != set(case_ids) or any(
        event.metadata_json.get("batch_fingerprint") != batch_fingerprint for event in events
    ):
        raise CustomerPriceTypeExportGateError(
            "message_id is already bound to another customer price-type batch"
        )


def _assert_existing_result(
    events: list[CustomerPriceTypeCaseEvent],
    *,
    case_ids: tuple[int, ...],
    result_fingerprint: str,
) -> None:
    if {event.case_id for event in events} != set(case_ids) or any(
        event.metadata_json.get("result_fingerprint") != result_fingerprint for event in events
    ):
        raise CustomerPriceTypeExportGateError(
            "result message_id is already bound to different result content"
        )


def _common_metadata_value(
    events: list[CustomerPriceTypeCaseEvent],
    key: str,
) -> str:
    values = {str(event.metadata_json.get(key) or "") for event in events}
    if len(values) != 1 or "" in values:
        raise CustomerPriceTypeExportGateError(f"persisted event metadata {key} is invalid")
    return next(iter(values))


def _case_gate_errors(
    *,
    case: CustomerPriceTypeCase,
    profile: CustomerPriceTypeProfile,
    snapshot: CustomerPriceTypeSnapshot,
) -> list[str]:
    errors: list[str] = []
    if case.approval_status != APPROVED_STATUS:
        errors.append("approval_status must be approved")
    if case.human_final_decision != APPROVED_FINAL_DECISION:
        errors.append(f"human_final_decision must be {APPROVED_FINAL_DECISION}")
    if not case.approver_name or not case.approver_name.strip():
        errors.append("approver_name is required")
    if case.approved_at is None:
        errors.append("approved_at is required")
    if case.approved_snapshot_hash != snapshot.snapshot_hash:
        errors.append("approved_snapshot_hash does not match current snapshot")
    if not re.fullmatch(r"[0-9a-f]{64}", snapshot.snapshot_hash):
        errors.append("snapshot_hash must be a lowercase SHA-256 value")
    if snapshot.profile_id != profile.id:
        errors.append("case snapshot belongs to another profile")
    if snapshot.counterparty_ref != profile.counterparty_ref:
        errors.append("snapshot counterparty_ref does not match profile")
    if snapshot.snapshot_month != case.snapshot_month:
        errors.append("case snapshot_month does not match snapshot")
    expected_case_key = f"{profile.counterparty_ref}:{case.snapshot_month:%Y-%m}"
    if case.case_key != expected_case_key:
        errors.append("case_key does not match profile and snapshot_month")
    if profile.latest_snapshot_id != snapshot.id:
        errors.append("case snapshot is not the latest profile snapshot")
    if profile.open_case_id != case.id:
        errors.append("case is not the open profile case")
    if case.ruleset_version != snapshot.ruleset_version:
        errors.append("case ruleset_version does not match snapshot")
    if case.system_recommendation != EXPECTED_RECOMMENDATION:
        errors.append(f"case recommendation must be {EXPECTED_RECOMMENDATION}")
    if snapshot.system_recommendation != EXPECTED_RECOMMENDATION:
        errors.append(f"snapshot recommendation must be {EXPECTED_RECOMMENDATION}")
    if case.recommended_price_type != TARGET_PRICE_TYPE:
        errors.append(f"case target must be {TARGET_PRICE_TYPE}")
    if snapshot.recommended_price_type != TARGET_PRICE_TYPE:
        errors.append(f"snapshot target must be {TARGET_PRICE_TYPE}")
    if snapshot.current_price_type != EXPECTED_CURRENT_PRICE_TYPE:
        errors.append(f"current price type must be {EXPECTED_CURRENT_PRICE_TYPE}")
    if snapshot.source_status != "ready":
        errors.append("snapshot source_status must be ready")
    if not snapshot.action_required:
        errors.append("snapshot must be actionable")
    if snapshot.conflicts:
        errors.append("snapshot conflicts must be empty")
    unexpected_stop_factors = sorted(set(snapshot.stop_factors) - ALLOWED_APPROVED_STOP_FACTORS)
    if unexpected_stop_factors:
        errors.append("blocking stop_factors: " + ", ".join(unexpected_stop_factors))
    if case.onec_readback_status != "not_requested":
        errors.append("onec_readback_status must be not_requested")
    if not profile.counterparty_name or not profile.counterparty_name.strip():
        errors.append("counterparty_name is required")
    try:
        one_c_guid_from_counterparty_ref(profile.counterparty_ref)
    except ValueError as error:
        errors.append(str(error))
    return errors
