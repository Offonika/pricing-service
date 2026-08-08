from __future__ import annotations

import hashlib
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.config import Settings, get_settings
from app.models.nomenclature_classification_operation import (
    NomenclatureClassificationOperation,
    NomenclatureClassificationOperationEvent,
    NomenclatureClassificationOperationItem,
)
from app.services.exporters.ut103_nomenclature_classifications import (
    DEFAULT_SOURCE,
    DEFAULT_TARGET,
    NomenclatureClassificationExchangeResult,
    NomenclatureClassificationIntentRow,
    NomenclatureClassificationUpdateMessage,
    NomenclatureClassificationUpdateRow,
    build_nomenclature_classification_message_id,
    build_nomenclature_classification_updates_xml,
    parse_category_guid_set,
    parse_nomenclature_classification_exchange_result,
    prepare_nomenclature_classification_command,
    result_fingerprint,
    rows_from_nomenclature_classification_payload,
    validate_nomenclature_classification_result,
    write_nomenclature_classification_updates_message,
)

ACTIVE_STATES = frozenset(
    {"pending_dry_run", "dry_run_sent", "dry_run_ok", "apply_sent", "applying"}
)


def register_nomenclature_classification_operation(
    db: Session,
    rows: tuple[NomenclatureClassificationIntentRow, ...],
    *,
    approved_by: str,
    requested_by: str,
    settings: Settings | None = None,
    source: str = DEFAULT_SOURCE,
    target: str = DEFAULT_TARGET,
) -> NomenclatureClassificationOperation:
    settings = settings or get_settings()
    _require_transport_enabled(settings)
    _require_approver_allowed(settings, approved_by)
    prepared, command_hash, canonical = prepare_nomenclature_classification_command(
        rows,
        approved_by=approved_by,
        source=source,
        target=target,
    )
    existing = db.scalar(
        select(NomenclatureClassificationOperation)
        .where(NomenclatureClassificationOperation.command_hash == command_hash)
        .options(selectinload(NomenclatureClassificationOperation.items))
    )
    if existing is not None:
        if existing.canonical_payload != canonical:
            raise ValueError("CommandHash is already bound to different canonical content")
        return existing

    keys = [row.idempotency_key for row in prepared]
    conflicts = list(
        db.scalars(
            select(NomenclatureClassificationOperationItem).where(
                NomenclatureClassificationOperationItem.idempotency_key.in_(keys)
            )
        )
    )
    if conflicts:
        raise ValueError("IdempotencyKey is already bound to another command")
    product_keys = [_active_product_key(row) for row in prepared]
    active = list(
        db.scalars(
            select(NomenclatureClassificationOperationItem).where(
                NomenclatureClassificationOperationItem.active_nomenclature_key.in_(product_keys)
            )
        )
    )
    if active:
        raise ValueError("another active classification operation exists for a product")

    operation = NomenclatureClassificationOperation(
        operation_id=str(uuid.uuid4()),
        command_hash=command_hash,
        state="pending_dry_run",
        approved_by=str(approved_by).strip(),
        requested_by=_required_actor(requested_by),
        source=source.strip(),
        target=target.strip(),
        canonical_payload=canonical,
    )
    for row in prepared:
        operation.items.append(
            NomenclatureClassificationOperationItem(
                idempotency_key=row.idempotency_key,
                decision_hash=row.decision_hash,
                nomenclature_code=row.nomenclature_code,
                nomenclature_guid=row.nomenclature_guid,
                active_nomenclature_key=_active_product_key(row),
                canonical_payload=_row_payload(row),
            )
        )
    db.add(operation)
    db.flush()
    _record_event(
        db,
        operation,
        event_key=f"registered:{operation.operation_id}",
        event_type="registered",
        fingerprint=command_hash,
        payload={"items": len(prepared), "requested_by": operation.requested_by},
    )
    db.commit()
    return _load_operation(db, operation.operation_id)


def get_nomenclature_classification_status(db: Session, operation_id: str) -> dict[str, Any]:
    return _operation_status(_load_operation(db, operation_id))


def request_nomenclature_classification_apply(
    db: Session,
    operation_id: str,
    *,
    requested_by: str,
    settings: Settings | None = None,
) -> NomenclatureClassificationOperation:
    settings = settings or get_settings()
    _require_transport_enabled(settings)
    operation = _load_operation(db, operation_id)
    if operation.state != "dry_run_ok":
        raise ValueError("apply may be requested only after exact successful dry_run")
    _require_approver_allowed(settings, operation.approved_by)
    _require_pilot_allowed(settings, operation)
    _assert_persisted_command_hash(operation)
    operation.apply_requested_at = _utcnow_naive()
    operation.apply_requested_by = _required_actor(requested_by)
    _record_event(
        db,
        operation,
        event_key=f"apply-requested:{operation.operation_id}",
        event_type="apply_requested",
        fingerprint=operation.command_hash,
        payload={"requested_by": operation.apply_requested_by},
    )
    db.commit()
    return _load_operation(db, operation_id)


def cancel_nomenclature_classification_operation(
    db: Session,
    operation_id: str,
    *,
    requested_by: str,
    confirm_read_only_reconciled: bool = False,
) -> NomenclatureClassificationOperation:
    operation = _load_operation(db, operation_id)
    if operation.state == "applied":
        raise ValueError("an applied operation cannot be cancelled")
    if operation.state == "cancelled":
        return operation
    ambiguous = operation.state in {"apply_sent", "applying"} or operation.failure_kind in {
        "ambiguous_apply",
        "partial_apply",
        "partial_dry_run",
        "request_identity_conflict",
        "result_identity_conflict",
        "readback_failed",
    }
    if ambiguous and not confirm_read_only_reconciled:
        raise ValueError("ambiguous apply requires confirmed read-only reconciliation")
    previous_state = operation.state
    operation.state = "cancelled"
    operation.failure_kind = None
    operation.last_error = f"cancelled by {_required_actor(requested_by)}"
    _release_active_product_locks(operation)
    _record_event(
        db,
        operation,
        event_key=f"cancelled:{operation.operation_id}",
        event_type="cancelled",
        fingerprint=operation.command_hash,
        payload={
            "requested_by": requested_by,
            "state_from": previous_state,
            "state_to": operation.state,
        },
    )
    db.commit()
    return _load_operation(db, operation_id)


def run_nomenclature_classification_cycle(
    db: Session,
    *,
    exchange_root: str | Path,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    _require_transport_enabled(settings)
    if not settings.nomenclature_classification_worker_enabled:
        raise RuntimeError("nomenclature classification worker is disabled")
    current = _as_aware_utc(now or datetime.now(UTC))
    root = Path(exchange_root)
    summary = {"results": 0, "processed": 0, "errors": []}

    result_dir = root / "from_1c" / "new"
    if result_dir.exists():
        for path in sorted(result_dir.glob("nomenclature_classifications_*.result.xml")):
            try:
                _consume_result_path(db, path, now=current)
                summary["results"] += 1
            except (RuntimeError, ValueError) as error:
                db.rollback()
                summary["errors"].append(f"{path.name}: {error}")

    operations = list(
        db.scalars(
            select(NomenclatureClassificationOperation)
            .where(NomenclatureClassificationOperation.state.in_(ACTIVE_STATES))
            .options(selectinload(NomenclatureClassificationOperation.items))
            .order_by(NomenclatureClassificationOperation.id)
            .limit(settings.nomenclature_classification_poll_limit)
        )
    )
    for operation in operations:
        try:
            _process_operation(db, operation, root, settings=settings, now=current)
            summary["processed"] += 1
        except (OSError, RuntimeError, ValueError) as error:
            db.rollback()
            summary["errors"].append(f"{operation.operation_id}: {error}")
    return summary


def _process_operation(
    db: Session,
    operation: NomenclatureClassificationOperation,
    exchange_root: Path,
    *,
    settings: Settings,
    now: datetime,
) -> None:
    if operation.state == "pending_dry_run":
        _send_message(db, operation, exchange_root, mode="dry_run", now=now)
        return
    if operation.state == "dry_run_sent":
        if _timed_out(
            operation.dry_run_sent_at,
            now,
            settings.nomenclature_classification_result_timeout_seconds,
        ):
            if (
                operation.dry_run_attempts
                < settings.nomenclature_classification_max_dry_run_attempts
            ):
                _send_message(db, operation, exchange_root, mode="dry_run", now=now, retry=True)
            else:
                _mark_failed(
                    db,
                    operation,
                    "dry_run result was not received after safe retries",
                    failure_kind="dry_run_timeout",
                    ambiguous=False,
                )
        return
    if operation.state == "dry_run_ok":
        if operation.apply_requested_at is None:
            if not settings.nomenclature_classification_auto_apply_enabled:
                return
            _require_approver_allowed(settings, operation.approved_by)
            _require_pilot_allowed(settings, operation)
            operation.apply_requested_at = _utcnow_naive(now)
            operation.apply_requested_by = "auto_apply"
            _record_event(
                db,
                operation,
                event_key=f"apply-requested:{operation.operation_id}",
                event_type="apply_requested",
                fingerprint=operation.command_hash,
                payload={"requested_by": operation.apply_requested_by},
            )
            db.commit()
        _assert_persisted_command_hash(operation)
        _send_message(db, operation, exchange_root, mode="apply", now=now)
        return
    if operation.state not in {"apply_sent", "applying"}:
        return
    if operation.readback_message_id:
        if _timed_out(
            operation.readback_sent_at,
            now,
            settings.nomenclature_classification_result_timeout_seconds,
        ):
            if (
                operation.readback_attempts
                < settings.nomenclature_classification_max_readback_attempts
            ):
                _send_message(db, operation, exchange_root, mode="readback", now=now, retry=True)
            else:
                _mark_failed(
                    db,
                    operation,
                    "recovery readback was not received; apply was not resent",
                    failure_kind="readback_failed",
                    ambiguous=True,
                )
        return
    if _timed_out(
        operation.apply_sent_at,
        now,
        settings.nomenclature_classification_result_timeout_seconds,
    ):
        previous_state = operation.state
        operation.state = "applying"
        operation.last_error = "apply result is missing; apply replay is blocked"
        _record_event(
            db,
            operation,
            event_key=f"state:{operation.operation_id}:applying",
            event_type="state_transition",
            fingerprint=operation.command_hash,
            payload={"state_from": previous_state, "state_to": operation.state},
        )
        db.commit()
        _send_message(db, operation, exchange_root, mode="readback", now=now)


def _send_message(
    db: Session,
    operation: NomenclatureClassificationOperation,
    exchange_root: Path,
    *,
    mode: str,
    now: datetime,
    retry: bool = False,
) -> None:
    attr = {
        "dry_run": "dry_run_message_id",
        "apply": "apply_message_id",
        "readback": "readback_message_id",
    }[mode]
    message_id = getattr(operation, attr) or build_nomenclature_classification_message_id(
        operation.operation_id, operation.command_hash, mode
    )
    setattr(operation, attr, message_id)
    message = _message_for_operation(operation, mode=mode)
    payload = build_nomenclature_classification_updates_xml(message)
    fingerprint = hashlib.sha256(payload).hexdigest()
    previous_state = operation.state
    if mode == "dry_run":
        operation.state = "dry_run_sent"
        operation.dry_run_attempts += 1
        operation.dry_run_sent_at = _utcnow_naive(now)
        attempt = operation.dry_run_attempts
    elif mode == "apply":
        if retry or operation.apply_attempts:
            raise RuntimeError("apply replay is forbidden")
        operation.state = "apply_sent"
        operation.apply_attempts = 1
        operation.apply_sent_at = _utcnow_naive(now)
        attempt = operation.apply_attempts
    else:
        operation.state = "applying"
        operation.readback_attempts += 1
        operation.readback_sent_at = _utcnow_naive(now)
        attempt = operation.readback_attempts
    operation.last_error = f"publishing {mode}; durable state committed before file write"
    _record_event(
        db,
        operation,
        event_key=f"request:{mode}:{message_id}:{attempt}",
        event_type="request_persisted",
        mode=mode,
        message_id=message_id,
        fingerprint=fingerprint,
        payload={
            "attempt": attempt,
            "retry": retry,
            "state_from": previous_state,
            "state_to": operation.state,
        },
    )
    db.commit()
    try:
        write_nomenclature_classification_updates_message(exchange_root, message)
    except FileExistsError as error:
        path = Path(error.filename or str(error))
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != fingerprint:
            _mark_failed(
                db,
                operation,
                f"existing ready file differs from persisted {mode} request",
                failure_kind="request_identity_conflict",
                ambiguous=True,
            )
            return
    operation.last_error = None
    db.commit()


def _consume_result_path(db: Session, path: Path, *, now: datetime) -> None:
    result = parse_nomenclature_classification_exchange_result(path)
    operation = db.scalar(
        select(NomenclatureClassificationOperation)
        .where(NomenclatureClassificationOperation.operation_id == result.operation_id)
        .options(selectinload(NomenclatureClassificationOperation.items))
    )
    if operation is None:
        raise ValueError("result OperationId is unknown")
    fingerprint = result_fingerprint(result)
    event_key = f"result:{result.mode}:{result.message_id}"
    existing = db.scalar(
        select(NomenclatureClassificationOperationEvent).where(
            NomenclatureClassificationOperationEvent.event_key == event_key
        )
    )
    if existing is not None:
        if existing.fingerprint != fingerprint:
            _mark_failed(
                db,
                operation,
                "the same result MessageId has different content",
                failure_kind="result_identity_conflict",
                ambiguous=True,
            )
            _quarantine_result(path)
            return
        _archive_result(path)
        return
    expected_id = {
        "dry_run": operation.dry_run_message_id,
        "apply": operation.apply_message_id,
        "readback": operation.readback_message_id,
    }[result.mode]
    expected_states = {
        "dry_run": {"dry_run_sent"},
        "apply": {"apply_sent", "applying", "applied"},
        "readback": {"applying", "applied"},
    }[result.mode]
    if result.message_id != expected_id or operation.state not in expected_states:
        _mark_failed(
            db,
            operation,
            "result mode or MessageId does not match the durable state",
            failure_kind="result_identity_conflict",
            ambiguous=True,
        )
        _quarantine_result(path)
        return
    if result.mode == "apply" and operation.state == "apply_sent":
        previous_state = operation.state
        operation.state = "applying"
        _record_event(
            db,
            operation,
            event_key=f"state:{operation.operation_id}:applying",
            event_type="state_transition",
            fingerprint=operation.command_hash,
            payload={"state_from": previous_state, "state_to": operation.state},
        )
    message = _message_for_operation(operation, mode=result.mode)
    try:
        validate_nomenclature_classification_result(message, result)
        _validate_mode_result(operation, result)
    except ValueError as error:
        _record_result_event(db, operation, result, fingerprint, accepted=False, error=str(error))
        _mark_failed(
            db,
            operation,
            str(error),
            failure_kind="result_identity_conflict",
            ambiguous=True,
        )
        _quarantine_result(path)
        return
    _record_result_event(db, operation, result, fingerprint, accepted=True)
    _store_item_results(operation, result)
    operation.last_result_status = result.status
    operation.last_result_at = _utcnow_naive(now)
    if result.mode == "dry_run":
        if result.ok and all(
            item.result in {"validated", "already_actual"} for item in result.item_results
        ):
            operation.state = "dry_run_ok"
            operation.last_error = None
            _record_event(
                db,
                operation,
                event_key=f"state:{operation.operation_id}:dry_run_ok",
                event_type="state_transition",
                fingerprint=operation.command_hash,
                payload={"state_from": "dry_run_sent", "state_to": operation.state},
            )
        else:
            partial = result.status == "partial" or (result.loaded > 0 and result.failed > 0)
            _mark_failed(
                db,
                operation,
                result.errors or "dry_run was rejected",
                failure_kind="partial_dry_run" if partial else "dry_run_rejected",
                ambiguous=partial,
            )
    elif result.mode == "apply":
        if result.ok and all(
            item.result in {"applied", "already_actual"} for item in result.item_results
        ):
            _mark_applied(db, operation, now)
        else:
            _mark_failed(
                db,
                operation,
                result.errors or "apply was partial or rejected",
                failure_kind="partial_apply",
                ambiguous=True,
            )
    else:
        if result.ok and all(item.result == "already_actual" for item in result.item_results):
            _mark_applied(db, operation, now)
        else:
            _mark_failed(
                db,
                operation,
                result.errors or "recovery readback did not prove exact apply",
                failure_kind="readback_failed",
                ambiguous=True,
            )
    db.commit()
    _archive_result(path)


def _validate_mode_result(
    operation: NomenclatureClassificationOperation,
    result: NomenclatureClassificationExchangeResult,
) -> None:
    rows = {
        row.idempotency_key: row
        for row in rows_from_nomenclature_classification_payload(operation.canonical_payload)
    }
    for item in result.item_results:
        if item.result not in {"validated", "applied", "already_actual"}:
            continue
        row = rows[item.idempotency_key]
        if result.mode == "dry_run":
            expected_kind = (
                row.target_kind.guid if item.result == "already_actual" else row.expected_kind.guid
            )
            expected_group = (
                row.target_group.guid
                if item.result == "already_actual"
                else row.expected_group.guid
            )
            if item.readback_kind_guid.lower() != expected_kind.lower():
                raise ValueError("dry_run readback kind does not match expected state")
            if item.readback_group_guid.lower() != expected_group.lower():
                raise ValueError("dry_run readback group does not match expected state")
        elif item.result not in {"applied", "already_actual"}:
            raise ValueError(f"{result.mode} does not prove applied state")


def _record_result_event(
    db: Session,
    operation: NomenclatureClassificationOperation,
    result: NomenclatureClassificationExchangeResult,
    fingerprint: str,
    *,
    accepted: bool,
    error: str = "",
) -> None:
    _record_event(
        db,
        operation,
        event_key=f"result:{result.mode}:{result.message_id}",
        event_type="result_accepted" if accepted else "result_rejected",
        mode=result.mode,
        message_id=result.message_id,
        fingerprint=fingerprint,
        payload={
            "accepted": accepted,
            "error": error,
            "failed": result.failed,
            "loaded": result.loaded,
            "status": result.status,
        },
    )


def _record_event(
    db: Session,
    operation: NomenclatureClassificationOperation,
    *,
    event_key: str,
    event_type: str,
    fingerprint: str | None = None,
    mode: str | None = None,
    message_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> NomenclatureClassificationOperationEvent:
    existing = db.scalar(
        select(NomenclatureClassificationOperationEvent).where(
            NomenclatureClassificationOperationEvent.event_key == event_key
        )
    )
    if existing is not None:
        if existing.fingerprint != fingerprint:
            raise ValueError("event key is already bound to another fingerprint")
        return existing
    event = NomenclatureClassificationOperationEvent(
        operation_pk=operation.id,
        event_key=event_key,
        event_type=event_type,
        mode=mode,
        message_id=message_id,
        fingerprint=fingerprint,
        payload=payload,
    )
    db.add(event)
    return event


def _message_for_operation(
    operation: NomenclatureClassificationOperation,
    *,
    mode: str,
) -> NomenclatureClassificationUpdateMessage:
    message_id = {
        "dry_run": operation.dry_run_message_id,
        "apply": operation.apply_message_id,
        "readback": operation.readback_message_id,
    }[mode]
    if not message_id:
        raise RuntimeError(f"{mode} MessageId is not prepared")
    return NomenclatureClassificationUpdateMessage(
        operation_id=operation.operation_id,
        command_hash=operation.command_hash,
        message_id=message_id,
        rows=rows_from_nomenclature_classification_payload(operation.canonical_payload),
        approved_by=operation.approved_by,
        mode=mode,
        source=operation.source,
        target=operation.target,
    )


def _assert_persisted_command_hash(operation: NomenclatureClassificationOperation) -> None:
    stored = rows_from_nomenclature_classification_payload(operation.canonical_payload)
    intents = tuple(_intent_from_update(row) for row in stored)
    rows, command_hash, canonical = prepare_nomenclature_classification_command(
        intents,
        approved_by=operation.approved_by,
        source=operation.source,
        target=operation.target,
    )
    if (
        rows != stored
        or command_hash != operation.command_hash
        or canonical != operation.canonical_payload
    ):
        raise ValueError("persisted command content no longer matches CommandHash")


def _store_item_results(
    operation: NomenclatureClassificationOperation,
    result: NomenclatureClassificationExchangeResult,
) -> None:
    by_key = {item.idempotency_key: item for item in operation.items}
    for result_item in result.item_results:
        item = by_key[result_item.idempotency_key]
        item.last_result = result_item.result
        item.old_category_guids = sorted(parse_category_guid_set(result_item.old_category_guids))
        item.projected_category_guids = sorted(
            parse_category_guid_set(result_item.projected_category_guids)
        )
        item.readback_category_guids = sorted(
            parse_category_guid_set(result_item.readback_category_guids)
        )


def _mark_applied(
    db: Session,
    operation: NomenclatureClassificationOperation,
    now: datetime,
) -> None:
    previous_state = operation.state
    operation.state = "applied"
    operation.applied_at = _utcnow_naive(now)
    operation.failure_kind = None
    operation.last_error = None
    _release_active_product_locks(operation)
    _record_event(
        db,
        operation,
        event_key=f"applied:{operation.operation_id}",
        event_type="applied",
        fingerprint=operation.command_hash,
        payload={"state_from": previous_state, "state_to": operation.state},
    )


def _mark_failed(
    db: Session,
    operation: NomenclatureClassificationOperation,
    message: str,
    *,
    failure_kind: str,
    ambiguous: bool,
) -> None:
    previous_state = operation.state
    operation.state = "failed"
    operation.failure_kind = failure_kind
    operation.last_error = str(message)[:2000]
    if not ambiguous:
        _release_active_product_locks(operation)
    event_key = f"failed:{operation.operation_id}:{failure_kind}"
    _record_event(
        db,
        operation,
        event_key=event_key,
        event_type="failed",
        fingerprint=operation.command_hash,
        payload={
            "ambiguous": ambiguous,
            "message": operation.last_error,
            "state_from": previous_state,
            "state_to": operation.state,
        },
    )
    db.commit()


def _release_active_product_locks(operation: NomenclatureClassificationOperation) -> None:
    for item in operation.items:
        item.active_nomenclature_key = None


def _archive_result(path: Path) -> Path:
    archive = path.parent.parent / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    target = archive / path.name
    if target.exists():
        target = archive / f"{path.stem}.{uuid.uuid4().hex}.xml"
    path.replace(target)
    return target


def _quarantine_result(path: Path) -> Path:
    quarantine = path.parent.parent / "error"
    quarantine.mkdir(parents=True, exist_ok=True)
    target = quarantine / path.name
    if target.exists():
        target = quarantine / f"{path.stem}.{uuid.uuid4().hex}.xml"
    path.replace(target)
    return target


def _load_operation(db: Session, operation_id: str) -> NomenclatureClassificationOperation:
    normalized = str(uuid.UUID(operation_id)).lower()
    operation = db.scalar(
        select(NomenclatureClassificationOperation)
        .where(NomenclatureClassificationOperation.operation_id == normalized)
        .options(
            selectinload(NomenclatureClassificationOperation.items),
            selectinload(NomenclatureClassificationOperation.events),
        )
    )
    if operation is None:
        raise ValueError("nomenclature classification operation was not found")
    return operation


def _operation_status(operation: NomenclatureClassificationOperation) -> dict[str, Any]:
    return {
        "operation_id": operation.operation_id,
        "command_hash": operation.command_hash,
        "state": operation.state,
        "approved_by": operation.approved_by,
        "dry_run_message_id": operation.dry_run_message_id,
        "apply_message_id": operation.apply_message_id,
        "readback_message_id": operation.readback_message_id,
        "dry_run_attempts": operation.dry_run_attempts,
        "apply_attempts": operation.apply_attempts,
        "readback_attempts": operation.readback_attempts,
        "failure_kind": operation.failure_kind,
        "last_error": operation.last_error,
        "items": [
            {
                "idempotency_key": item.idempotency_key,
                "decision_hash": item.decision_hash,
                "nomenclature_code": item.nomenclature_code,
                "nomenclature_guid": item.nomenclature_guid,
                "last_result": item.last_result,
            }
            for item in operation.items
        ],
    }


def _require_transport_enabled(settings: Settings) -> None:
    if not settings.nomenclature_classification_transport_enabled:
        raise RuntimeError("nomenclature classification transport is disabled")


def _require_approver_allowed(settings: Settings, approved_by: str) -> None:
    allowed = {
        str(value).strip() for value in settings.nomenclature_classification_approved_by_allowlist
    }
    if str(approved_by).strip() not in allowed:
        raise ValueError("ApprovedBy is not in the configured allowlist")


def _require_pilot_allowed(
    settings: Settings,
    operation: NomenclatureClassificationOperation,
) -> None:
    pilots = {
        str(value).strip().upper()
        for value in settings.nomenclature_classification_pilot_nomenclature_codes
    }
    codes = {item.nomenclature_code.upper() for item in operation.items}
    if not codes or not codes <= pilots:
        raise ValueError("operation contains nomenclature outside the pilot allowlist")


def _required_actor(value: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise ValueError("requested_by is required")
    if len(normalized) > 150:
        raise ValueError("requested_by cannot exceed 150 characters")
    return normalized


def _active_product_key(row: NomenclatureClassificationUpdateRow) -> str:
    return f"guid:{row.nomenclature_guid}"


def _row_payload(row: NomenclatureClassificationUpdateRow) -> dict[str, Any]:
    return asdict(row)


def _intent_from_update(
    row: NomenclatureClassificationUpdateRow,
) -> NomenclatureClassificationIntentRow:
    values = asdict(row)
    values.pop("decision_hash")
    for name in (
        "expected_kind",
        "target_kind",
        "expected_group",
        "target_group",
        "expected_category",
        "target_category",
    ):
        reference = getattr(row, name)
        values[name] = reference
    return NomenclatureClassificationIntentRow(**values)


def _timed_out(value: datetime | None, now: datetime, timeout_seconds: int) -> bool:
    if value is None:
        return False
    return (_as_aware_utc(now) - _as_aware_utc(value)).total_seconds() >= timeout_seconds


def _as_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _utcnow_naive(value: datetime | None = None) -> datetime:
    return _as_aware_utc(value or datetime.now(UTC)).replace(tzinfo=None)
