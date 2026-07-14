from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session, joinedload

from app.core.config import Settings, get_settings
from app.models import ExpertiseCase, ExpertiseCaseAttachment, ExpertiseCaseEvent
from app.services import expertise_bitrix
from app.services.expertise_sla import delivery_deadline_at, normalize_datetime, review_deadline_at

STATUS_CREATED = "created"
STATUS_RECEIVED_BY_OKK = "received_by_okk"
STATUS_UNDER_REVIEW = "under_review"
STATUS_DECISION_READY = "decision_ready"
STATUS_CLIENT_NOTIFIED = "client_notified"
STATUS_RETURNED_TO_CENTRAL_DEFECT = "returned_to_central_defect"
STATUS_RETURNED_TO_STORE = "returned_to_store"
STATUS_MANUAL_REVIEW = "manual_review"

EVENT_SYNCED = "synced"
EVENT_RECEIVED_BY_OKK = "received_by_okk"
EVENT_MOVED_TO_REVIEW = "moved_to_review"
EVENT_DECISION_RECORDED = "decision_recorded"
EVENT_CLIENT_NOTIFIED = "client_notified"
EVENT_RETURNED_TO_CENTRAL_DEFECT = "returned_to_central_defect"
EVENT_RETURNED_TO_STORE = "returned_to_store"
EVENT_MANUAL_REVIEW_REQUIRED = "manual_review_required"

DECISION_APPROVED = "approved"
DECISION_REJECTED = "rejected"
DECISION_CODES = {DECISION_APPROVED, DECISION_REJECTED}
COMPLETION_OUTCOMES = {
    STATUS_RETURNED_TO_CENTRAL_DEFECT,
    STATUS_RETURNED_TO_STORE,
}
TERMINAL_STATUSES = set(COMPLETION_OUTCOMES)
REVIEW_PHASE_STATUSES = {
    STATUS_RECEIVED_BY_OKK,
    STATUS_UNDER_REVIEW,
    STATUS_MANUAL_REVIEW,
}
SYNC_DECISION_SOURCE_STATUSES = {
    STATUS_CREATED,
    STATUS_RECEIVED_BY_OKK,
    STATUS_UNDER_REVIEW,
    STATUS_MANUAL_REVIEW,
}
NO_DUE_AT_STATUSES = {
    STATUS_CLIENT_NOTIFIED,
    STATUS_RETURNED_TO_CENTRAL_DEFECT,
    STATUS_RETURNED_TO_STORE,
}
KNOWN_STATUSES = {
    STATUS_CREATED,
    STATUS_RECEIVED_BY_OKK,
    STATUS_UNDER_REVIEW,
    STATUS_DECISION_READY,
    STATUS_CLIENT_NOTIFIED,
    STATUS_RETURNED_TO_CENTRAL_DEFECT,
    STATUS_RETURNED_TO_STORE,
    STATUS_MANUAL_REVIEW,
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _http_error(status: int, detail) -> HTTPException:
    return HTTPException(status_code=status, detail=detail)


def _normalize_datetime(value: datetime | None) -> datetime | None:
    return normalize_datetime(value)


def _serialize_case(case_row: ExpertiseCase) -> dict:
    return {
        "id": case_row.id,
        "external_id": case_row.external_id,
        "onec_expertise_ref": case_row.onec_expertise_ref,
        "onec_expertise_number": case_row.onec_expertise_number,
        "created_at_source": case_row.created_at_source,
        "organization_ref": case_row.organization_ref,
        "contract_ref": case_row.contract_ref,
        "linked_sale_ref": case_row.linked_sale_ref,
        "linked_sale_number": case_row.linked_sale_number,
        "store_external_id": case_row.store_external_id,
        "store_name": case_row.store_name,
        "customer_name": case_row.customer_name,
        "customer_phone": case_row.customer_phone,
        "problem_summary": case_row.problem_summary,
        "current_status": case_row.current_status,
        "decision_code": case_row.decision_code,
        "decision_label": case_row.decision_label,
        "decision_comment": case_row.decision_comment,
        "linked_customer_order_ref": case_row.linked_customer_order_ref,
        "linked_customer_order_number": case_row.linked_customer_order_number,
        "client_notified": case_row.client_notified,
        "due_at": case_row.due_at,
        "owner_user_external_id": case_row.owner_user_external_id,
        "bitrix_entity_id": case_row.bitrix_entity_id,
        "bitrix_disk_folder_id": case_row.bitrix_disk_folder_id,
        "bitrix_disk_folder_url": case_row.bitrix_disk_folder_url,
        "bitrix_notify_task_id": case_row.bitrix_notify_task_id,
        "bitrix_last_sync_at": case_row.bitrix_last_sync_at,
        "bitrix_last_error": case_row.bitrix_last_error,
        "payload": case_row.payload,
        "created_at": case_row.created_at,
        "updated_at": case_row.updated_at,
        "attachments": [
            {
                "id": attachment.id,
                "attachment_kind": attachment.attachment_kind,
                "storage_ref": attachment.storage_ref,
                "comment": attachment.comment,
                "created_at": attachment.created_at,
            }
            for attachment in case_row.attachments
        ],
    }


def _serialize_event(event: ExpertiseCaseEvent) -> dict:
    return {
        "id": event.id,
        "event_type": event.event_type,
        "event_at": event.event_at,
        "actor_external_id": event.actor_external_id,
        "source": event.source,
        "comment": event.comment,
        "meta": event.meta,
        "created_at": event.created_at,
    }


def _get_case(session: Session, case_id: int) -> ExpertiseCase:
    case_row = session.scalar(
        select(ExpertiseCase)
        .where(ExpertiseCase.id == case_id)
        .options(joinedload(ExpertiseCase.attachments))
    )
    if case_row is None:
        raise _http_error(404, "expertise case not found")
    return case_row


def _resolve_case_for_sync(session: Session, item: dict) -> ExpertiseCase | None:
    row = session.scalar(
        select(ExpertiseCase).where(ExpertiseCase.external_id == item["external_id"])
    )
    if row is not None:
        return row
    if item.get("onec_expertise_ref"):
        return session.scalar(
            select(ExpertiseCase).where(
                ExpertiseCase.onec_expertise_ref == item["onec_expertise_ref"]
            )
        )
    return None


def _replace_attachments(case_row: ExpertiseCase, attachments: list[dict] | None) -> None:
    if attachments is None:
        return
    case_row.attachments.clear()
    for attachment in attachments:
        case_row.attachments.append(
            ExpertiseCaseAttachment(
                attachment_kind=attachment["attachment_kind"],
                storage_ref=attachment["storage_ref"],
                comment=attachment.get("comment"),
            )
        )


def _coalesce_text(*values: object) -> str | None:
    for value in values:
        if not isinstance(value, str):
            continue
        normalized = value.strip()
        if normalized:
            return normalized
    return None


def _first_payload_item(payload: dict | None) -> dict | None:
    if not isinstance(payload, dict):
        return None
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        return None
    first_item = items[0]
    return first_item if isinstance(first_item, dict) else None


def _resolve_problem_summary(explicit_value: str | None, payload: dict | None) -> str | None:
    if explicit_value is not None:
        return _coalesce_text(explicit_value)
    first_item = _first_payload_item(payload)
    return _coalesce_text(
        payload.get("manager_comment") if isinstance(payload, dict) else None,
        payload.get("КомментарийМенеджера") if isinstance(payload, dict) else None,
        first_item.get("return_reason_name") if first_item else None,
        first_item.get("ПричинаВозврата") if first_item else None,
    )


def _resolve_decision_comment(explicit_value: str | None, payload: dict | None) -> str | None:
    if explicit_value is not None:
        return _coalesce_text(explicit_value)
    if not isinstance(payload, dict):
        return None
    return _coalesce_text(
        payload.get("quality_comment"),
        payload.get("КомментарийОтделаБрака"),
    )


def _resolve_linked_customer_order_ref(item: dict) -> str | None:
    if "linked_customer_order_ref" in item:
        return _coalesce_text(item.get("linked_customer_order_ref"))
    payload = item.get("payload")
    if not isinstance(payload, dict):
        return None
    first_item = _first_payload_item(payload)
    return _coalesce_text(
        payload.get("linked_customer_order_ref"),
        first_item.get("linked_customer_order_ref") if first_item else None,
    )


def _resolve_linked_customer_order_number(item: dict) -> str | None:
    if "linked_customer_order_number" in item:
        return _coalesce_text(item.get("linked_customer_order_number"))
    payload = item.get("payload")
    if not isinstance(payload, dict):
        return None
    first_item = _first_payload_item(payload)
    return _coalesce_text(
        payload.get("linked_customer_order_number"),
        first_item.get("linked_customer_order_number") if first_item else None,
    )


def _validate_decision_code(decision_code: str | None) -> str | None:
    normalized = _coalesce_text(decision_code)
    if normalized is None:
        return None
    if normalized not in DECISION_CODES:
        raise _http_error(422, f"unsupported decision_code: {normalized}")
    return normalized


def _decision_label_from_code(decision_code: str | None) -> str | None:
    if decision_code == DECISION_APPROVED:
        return "Принято"
    if decision_code == DECISION_REJECTED:
        return "Отказано"
    return None


def _decision_code_from_label(decision_label: str | None) -> str | None:
    normalized = _coalesce_text(decision_label)
    if normalized is None:
        return None
    normalized_lower = normalized.lower()
    if normalized_lower in {"принято", DECISION_APPROVED}:
        return DECISION_APPROVED
    if normalized_lower in {"отказано", DECISION_REJECTED}:
        return DECISION_REJECTED
    return None


def _payload_posted(payload: dict | None) -> bool | None:
    if not isinstance(payload, dict):
        return None
    value = payload.get("posted")
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "t", "yes", "y"}:
            return True
        if normalized in {"0", "false", "f", "no", "n"}:
            return False
    return None


def _validate_completion_outcome(completion_outcome: str) -> str:
    normalized = _coalesce_text(completion_outcome)
    if normalized not in COMPLETION_OUTCOMES:
        raise _http_error(422, f"unsupported completion_outcome: {completion_outcome}")
    return normalized


def _build_idempotency_key(
    case_id: int,
    event_type: str,
    idempotency_key: str | None,
) -> str | None:
    if not idempotency_key:
        return None
    return f"{idempotency_key}:{case_id}:{event_type}"


def _get_existing_event_by_key(
    session: Session,
    case_id: int,
    event_type: str,
    idempotency_key: str | None,
) -> ExpertiseCaseEvent | None:
    event_key = _build_idempotency_key(case_id, event_type, idempotency_key)
    if event_key is None:
        return None
    return session.scalar(
        select(ExpertiseCaseEvent).where(ExpertiseCaseEvent.idempotency_key == event_key)
    )


def _resolve_ref(item: dict, *payload_keys: str) -> str | None:
    payload = item.get("payload")
    if not isinstance(payload, dict):
        return None
    return _coalesce_text(*(payload.get(key) for key in payload_keys))


def _resolve_organization_ref(item: dict) -> str | None:
    if "organization_ref" in item:
        return _coalesce_text(item.get("organization_ref"))
    return _resolve_ref(item, "organization_ref")


def _resolve_contract_ref(item: dict) -> str | None:
    if "contract_ref" in item:
        return _coalesce_text(item.get("contract_ref"))
    return _resolve_ref(item, "contract_ref")


def _resolve_linked_sale_ref(item: dict) -> str | None:
    if "linked_sale_ref" in item:
        return _coalesce_text(item.get("linked_sale_ref"))
    return _resolve_ref(item, "linked_sale_ref", "base_document_ref")


def _resolve_linked_sale_number(item: dict) -> str | None:
    if "linked_sale_number" in item:
        return _coalesce_text(item.get("linked_sale_number"))
    return _resolve_ref(item, "linked_sale_number", "base_document_number")


def _resolve_decision_label(item: dict) -> str | None:
    if "decision_label" in item:
        return _coalesce_text(item.get("decision_label"))
    payload = item.get("payload")
    first_item = _first_payload_item(payload)
    return _coalesce_text(
        payload.get("decision_label") if isinstance(payload, dict) else None,
        first_item.get("decision_label") if first_item else None,
    )


def _append_event(
    session: Session,
    *,
    case_row: ExpertiseCase,
    event_type: str,
    actor_external_id: str | None,
    source: str,
    comment: str | None,
    meta: dict | None,
    idempotency_key: str | None,
    event_at: datetime | None = None,
) -> ExpertiseCaseEvent:
    event = ExpertiseCaseEvent(
        expertise_case_id=case_row.id,
        event_type=event_type,
        event_at=event_at or utcnow(),
        actor_external_id=actor_external_id,
        source=source,
        comment=comment,
        idempotency_key=_build_idempotency_key(case_row.id, event_type, idempotency_key),
        meta=meta,
    )
    session.add(event)
    return event


def _is_operational_for_synced_decision(
    case_row: ExpertiseCase,
    *,
    previous_bitrix_entity_id: str | None,
) -> bool:
    if case_row.current_status != STATUS_CREATED:
        return True
    if case_row.bitrix_entity_id or previous_bitrix_entity_id:
        return True
    posted = _payload_posted(case_row.payload)
    if posted is not None:
        return not posted
    return False


def _synced_decision_idempotency_key(case_row: ExpertiseCase) -> str | None:
    if case_row.decision_code not in DECISION_CODES:
        return None
    stable_ref = _coalesce_text(case_row.onec_expertise_ref, case_row.external_id)
    if stable_ref is None:
        stable_ref = str(case_row.id)
    return f"sync-decision:{stable_ref}:{case_row.decision_code}"


def _apply_synced_decision_transition(
    session: Session,
    *,
    case_row: ExpertiseCase,
    settings: Settings,
    previous_bitrix_entity_id: str | None,
    action_at: datetime,
) -> bool:
    if case_row.decision_code not in DECISION_CODES:
        return False
    if case_row.client_notified or case_row.current_status in NO_DUE_AT_STATUSES:
        return False
    if case_row.current_status not in SYNC_DECISION_SOURCE_STATUSES | {STATUS_DECISION_READY}:
        return False
    if not _is_operational_for_synced_decision(
        case_row,
        previous_bitrix_entity_id=previous_bitrix_entity_id,
    ):
        return False

    if not case_row.decision_label:
        case_row.decision_label = _decision_label_from_code(case_row.decision_code)

    event_key = _synced_decision_idempotency_key(case_row)
    existing_event = _get_existing_event_by_key(
        session,
        case_row.id,
        EVENT_DECISION_RECORDED,
        event_key,
    )
    previous_status = case_row.current_status
    transitioned = False
    if case_row.current_status in SYNC_DECISION_SOURCE_STATUSES:
        case_row.current_status = STATUS_DECISION_READY
        case_row.due_at = _notify_due_at(anchor_at=action_at, settings=settings)
        transitioned = True

    if existing_event is None:
        _append_event(
            session,
            case_row=case_row,
            event_type=EVENT_DECISION_RECORDED,
            actor_external_id=None,
            source="sync",
            comment="Решение ОКК получено из 1С",
            meta={
                "from_status": previous_status,
                "to_status": case_row.current_status,
                "decision_code": case_row.decision_code,
                "decision_label": case_row.decision_label,
            },
            idempotency_key=event_key,
            event_at=action_at,
        )
    return transitioned


def _require_transition(
    case_row: ExpertiseCase,
    *,
    expected_status: str,
    action_name: str,
) -> None:
    if case_row.current_status != expected_status:
        raise _http_error(
            409,
            {
                "message": f"invalid transition for {action_name}",
                "current_status": case_row.current_status,
                "expected_status": expected_status,
            },
        )


@dataclass
class _SyncCounters:
    created: int = 0
    updated: int = 0


SYNCED_CASE_COLUMN_KEYS = (
    "external_id",
    "onec_expertise_ref",
    "onec_expertise_number",
    "created_at_source",
    "organization_ref",
    "contract_ref",
    "linked_sale_ref",
    "linked_sale_number",
    "store_external_id",
    "store_name",
    "customer_name",
    "customer_phone",
    "problem_summary",
    "current_status",
    "decision_code",
    "decision_label",
    "decision_comment",
    "linked_customer_order_ref",
    "linked_customer_order_number",
    "client_notified",
    "due_at",
    "owner_user_external_id",
    "payload",
)


def _case_has_synced_field_changes(case_row: ExpertiseCase) -> bool:
    state = inspect(case_row)
    return any(
        state.attrs[field_name].history.has_changes() for field_name in SYNCED_CASE_COLUMN_KEYS
    )


def _latest_event_at(
    session: Session,
    *,
    case_id: int,
    event_types: tuple[str, ...],
) -> datetime | None:
    event = session.scalar(
        select(ExpertiseCaseEvent)
        .where(
            ExpertiseCaseEvent.expertise_case_id == case_id,
            ExpertiseCaseEvent.event_type.in_(event_types),
        )
        .order_by(ExpertiseCaseEvent.event_at.desc(), ExpertiseCaseEvent.id.desc())
    )
    return None if event is None else _normalize_datetime(event.event_at)


def _created_anchor_at(case_row: ExpertiseCase) -> datetime:
    return (
        _normalize_datetime(case_row.created_at_source)
        or _normalize_datetime(case_row.created_at)
        or utcnow().replace(tzinfo=None)
    )


def _status_anchor_at(session: Session, case_row: ExpertiseCase) -> datetime:
    case_id = case_row.id
    if case_row.current_status == STATUS_RECEIVED_BY_OKK:
        event_at = _latest_event_at(session, case_id=case_id, event_types=(EVENT_RECEIVED_BY_OKK,))
    elif case_row.current_status == STATUS_UNDER_REVIEW:
        event_at = _latest_event_at(session, case_id=case_id, event_types=(EVENT_MOVED_TO_REVIEW,))
    elif case_row.current_status == STATUS_MANUAL_REVIEW:
        event_at = _latest_event_at(
            session,
            case_id=case_id,
            event_types=(
                EVENT_MANUAL_REVIEW_REQUIRED,
                EVENT_MOVED_TO_REVIEW,
                EVENT_RECEIVED_BY_OKK,
            ),
        )
    elif case_row.current_status == STATUS_DECISION_READY:
        event_at = _latest_event_at(
            session, case_id=case_id, event_types=(EVENT_DECISION_RECORDED,)
        )
    else:
        event_at = None
    return (
        event_at
        or _normalize_datetime(case_row.updated_at)
        or _normalize_datetime(case_row.created_at_source)
        or _normalize_datetime(case_row.created_at)
        or utcnow().replace(tzinfo=None)
    )


def _notify_due_at(
    *,
    anchor_at: datetime,
    settings: Settings,
) -> datetime:
    return _normalize_datetime(anchor_at) + timedelta(
        hours=settings.expertise_alarm_notify_warning_hours
    )


def _notify_due_at_for_case(
    *,
    session: Session,
    case_row: ExpertiseCase,
    settings: Settings,
) -> datetime:
    return _notify_due_at(anchor_at=_status_anchor_at(session, case_row), settings=settings)


def _delivery_due_at(
    *,
    case_row: ExpertiseCase,
    settings: Settings,
) -> datetime:
    deadline, _, _ = delivery_deadline_at(
        anchor_at=_created_anchor_at(case_row),
        store_external_id=case_row.store_external_id,
        store_group_map=settings.expertise_sla_store_group_map,
        delivery_days_map=settings.expertise_sla_delivery_days_map,
    )
    return deadline


def _review_due_at(
    *,
    anchor_at: datetime,
    case_row: ExpertiseCase,
    settings: Settings,
) -> datetime:
    deadline, _, _ = review_deadline_at(
        anchor_at=anchor_at,
        store_external_id=case_row.store_external_id,
        store_group_map=settings.expertise_sla_store_group_map,
        review_days_map=settings.expertise_sla_review_days_map,
    )
    return deadline


def _review_due_at_for_case(
    *,
    session: Session,
    case_row: ExpertiseCase,
    settings: Settings,
) -> datetime:
    return _review_due_at(
        anchor_at=_status_anchor_at(session, case_row),
        case_row=case_row,
        settings=settings,
    )


def _recalculate_due_at(
    session: Session,
    *,
    case_row: ExpertiseCase,
    settings: Settings,
    explicit_due_at: datetime | None = None,
    explicit_due_at_provided: bool = False,
) -> datetime | None:
    if case_row.current_status == STATUS_CREATED:
        return _delivery_due_at(case_row=case_row, settings=settings)
    if case_row.current_status in REVIEW_PHASE_STATUSES:
        if explicit_due_at_provided:
            return explicit_due_at
        return _review_due_at_for_case(session=session, case_row=case_row, settings=settings)
    if case_row.current_status == STATUS_DECISION_READY:
        return _notify_due_at_for_case(session=session, case_row=case_row, settings=settings)
    if case_row.current_status in NO_DUE_AT_STATUSES:
        return None
    if explicit_due_at_provided:
        return explicit_due_at
    return case_row.due_at


def _sync_case_with_bitrix(session: Session, *, case_id: int) -> None:
    try:
        expertise_bitrix.sync_case_to_bitrix(session, case_id=case_id)
    except Exception as error:
        row = session.scalar(select(ExpertiseCase).where(ExpertiseCase.id == case_id))
        if row is None:
            return
        row.bitrix_last_error = str(error)[:1000]
        session.commit()
        session.refresh(row)
        return
    session.commit()


def _sync_cases_with_bitrix(session: Session, *, case_ids: list[int]) -> None:
    unique_case_ids = list(dict.fromkeys(case_ids))
    for case_id in unique_case_ids:
        _sync_case_with_bitrix(session, case_id=case_id)


def sync_cases(session: Session, items: list[dict]) -> dict:
    settings = get_settings()
    counters = _SyncCounters()
    touched_case_ids: list[int] = []
    for item in items:
        row = _resolve_case_for_sync(session, item)
        if row is not None and item.get("idempotency_key"):
            existing = _get_existing_event_by_key(
                session,
                row.id,
                EVENT_SYNCED,
                item.get("idempotency_key"),
            )
            if existing is not None:
                continue

        is_created = row is None
        if row is None:
            initial_status = item.get("current_status") or STATUS_CREATED
            if initial_status not in KNOWN_STATUSES:
                raise _http_error(422, f"unsupported current_status: {initial_status}")
            row = ExpertiseCase(
                external_id=item["external_id"],
                current_status=initial_status,
                client_notified=item.get("client_notified", False),
            )
            session.add(row)
            counters.created += 1

        previous_bitrix_entity_id = row.bitrix_entity_id
        row.external_id = item["external_id"]
        row.onec_expertise_ref = item.get("onec_expertise_ref")
        row.onec_expertise_number = item.get("onec_expertise_number")
        row.created_at_source = item.get("created_at_source")
        organization_ref = _resolve_organization_ref(item)
        contract_ref = _resolve_contract_ref(item)
        linked_sale_ref = _resolve_linked_sale_ref(item)
        linked_sale_number = _resolve_linked_sale_number(item)
        row.owner_user_external_id = item.get("owner_user_external_id")
        row.payload = item.get("payload")
        row.problem_summary = _resolve_problem_summary(item.get("problem_summary"), row.payload)
        decision_label = _resolve_decision_label(item)
        row.decision_comment = _resolve_decision_comment(item.get("decision_comment"), row.payload)
        row.linked_customer_order_ref = _resolve_linked_customer_order_ref(item)
        row.linked_customer_order_number = _resolve_linked_customer_order_number(item)

        if "organization_ref" in item or organization_ref is not None:
            row.organization_ref = organization_ref
        if "contract_ref" in item or contract_ref is not None:
            row.contract_ref = contract_ref
        if "linked_sale_ref" in item or linked_sale_ref is not None:
            row.linked_sale_ref = linked_sale_ref
        if "linked_sale_number" in item or linked_sale_number is not None:
            row.linked_sale_number = linked_sale_number
        if "decision_label" in item or decision_label is not None:
            row.decision_label = decision_label

        if "store_external_id" in item:
            row.store_external_id = item.get("store_external_id")
        if "store_name" in item:
            row.store_name = item.get("store_name")
        if "customer_name" in item:
            row.customer_name = item.get("customer_name")
        if "customer_phone" in item:
            row.customer_phone = item.get("customer_phone")
        if "bitrix_entity_id" in item:
            row.bitrix_entity_id = item.get("bitrix_entity_id")
        if "bitrix_disk_folder_id" in item:
            row.bitrix_disk_folder_id = item.get("bitrix_disk_folder_id")
        if "bitrix_disk_folder_url" in item:
            row.bitrix_disk_folder_url = item.get("bitrix_disk_folder_url")
        if "bitrix_notify_task_id" in item:
            row.bitrix_notify_task_id = item.get("bitrix_notify_task_id")
        if "bitrix_last_sync_at" in item:
            row.bitrix_last_sync_at = item.get("bitrix_last_sync_at")
        if "bitrix_last_error" in item:
            row.bitrix_last_error = item.get("bitrix_last_error")
        if "decision_code" in item:
            row.decision_code = _validate_decision_code(item.get("decision_code"))
        elif decision_label is not None:
            inferred_decision_code = _decision_code_from_label(decision_label)
            if inferred_decision_code is not None:
                row.decision_code = inferred_decision_code
        if row.decision_code and not row.decision_label:
            row.decision_label = _decision_label_from_code(row.decision_code)

        _replace_attachments(row, item.get("attachments"))
        changed_before_flush = is_created or _case_has_synced_field_changes(row)
        session.flush()
        if _apply_synced_decision_transition(
            session,
            case_row=row,
            settings=settings,
            previous_bitrix_entity_id=previous_bitrix_entity_id,
            action_at=utcnow().replace(tzinfo=None),
        ):
            session.flush()
        row.due_at = _recalculate_due_at(
            session,
            case_row=row,
            settings=settings,
            explicit_due_at=item.get("due_at"),
            explicit_due_at_provided="due_at" in item,
        )
        should_sync_to_bitrix = changed_before_flush or _case_has_synced_field_changes(row)
        if not is_created and should_sync_to_bitrix:
            counters.updated += 1
        if should_sync_to_bitrix:
            touched_case_ids.append(row.id)

        _append_event(
            session,
            case_row=row,
            event_type=EVENT_SYNCED,
            actor_external_id=None,
            source="sync",
            comment=None,
            meta={"operation": "created" if is_created else "updated"},
            idempotency_key=item.get("idempotency_key"),
        )

    session.commit()
    _sync_cases_with_bitrix(session, case_ids=touched_case_ids)
    return counters.__dict__


def list_cases(
    session: Session,
    *,
    status: str | None = None,
    store_external_id: str | None = None,
    owner_user_external_id: str | None = None,
    overdue: bool | None = None,
    client_notified: bool | None = None,
) -> list[dict]:
    stmt = (
        select(ExpertiseCase)
        .options(joinedload(ExpertiseCase.attachments))
        .order_by(ExpertiseCase.due_at.asc(), ExpertiseCase.updated_at.desc())
    )
    rows = session.scalars(stmt).unique().all()
    now = _normalize_datetime(utcnow())
    payload = []
    for row in rows:
        if status and row.current_status != status:
            continue
        if store_external_id and row.store_external_id != store_external_id:
            continue
        if owner_user_external_id and row.owner_user_external_id != owner_user_external_id:
            continue
        if client_notified is not None and row.client_notified != client_notified:
            continue
        due_at = _normalize_datetime(row.due_at)
        is_overdue = (
            due_at is not None and due_at < now and row.current_status not in TERMINAL_STATUSES
        )
        if overdue is not None and is_overdue != overdue:
            continue
        payload.append(_serialize_case(row))
    return payload


def get_case(session: Session, *, case_id: int) -> dict:
    return _serialize_case(_get_case(session, case_id))


def get_case_history(session: Session, *, case_id: int) -> list[dict]:
    _get_case(session, case_id)
    events = session.scalars(
        select(ExpertiseCaseEvent)
        .where(ExpertiseCaseEvent.expertise_case_id == case_id)
        .order_by(ExpertiseCaseEvent.event_at.desc(), ExpertiseCaseEvent.id.desc())
    ).all()
    return [_serialize_event(event) for event in events]


def receive_case(
    session: Session,
    *,
    case_id: int,
    actor_external_id: str,
    comment: str | None,
    idempotency_key: str | None,
) -> dict:
    settings = get_settings()
    row = _get_case(session, case_id)
    existing = _get_existing_event_by_key(session, case_id, EVENT_RECEIVED_BY_OKK, idempotency_key)
    if existing is not None:
        _sync_case_with_bitrix(session, case_id=case_id)
        return _serialize_case(row)
    _require_transition(row, expected_status=STATUS_CREATED, action_name="receive")
    action_at = utcnow().replace(tzinfo=None)
    previous_status = row.current_status
    row.current_status = STATUS_RECEIVED_BY_OKK
    row.due_at = _review_due_at(anchor_at=action_at, case_row=row, settings=settings)
    _append_event(
        session,
        case_row=row,
        event_type=EVENT_RECEIVED_BY_OKK,
        actor_external_id=actor_external_id,
        source="api",
        comment=comment,
        meta={"from_status": previous_status, "to_status": row.current_status},
        idempotency_key=idempotency_key,
        event_at=action_at,
    )
    session.commit()
    _sync_case_with_bitrix(session, case_id=case_id)
    session.refresh(row)
    return _serialize_case(row)


def start_review(
    session: Session,
    *,
    case_id: int,
    actor_external_id: str,
    comment: str | None,
    idempotency_key: str | None,
) -> dict:
    settings = get_settings()
    row = _get_case(session, case_id)
    existing = _get_existing_event_by_key(session, case_id, EVENT_MOVED_TO_REVIEW, idempotency_key)
    if existing is not None:
        _sync_case_with_bitrix(session, case_id=case_id)
        return _serialize_case(row)
    _require_transition(row, expected_status=STATUS_RECEIVED_BY_OKK, action_name="start-review")
    action_at = utcnow().replace(tzinfo=None)
    previous_status = row.current_status
    row.current_status = STATUS_UNDER_REVIEW
    row.due_at = _review_due_at(anchor_at=action_at, case_row=row, settings=settings)
    _append_event(
        session,
        case_row=row,
        event_type=EVENT_MOVED_TO_REVIEW,
        actor_external_id=actor_external_id,
        source="api",
        comment=comment,
        meta={"from_status": previous_status, "to_status": row.current_status},
        idempotency_key=idempotency_key,
        event_at=action_at,
    )
    session.commit()
    _sync_case_with_bitrix(session, case_id=case_id)
    session.refresh(row)
    return _serialize_case(row)


def record_decision(
    session: Session,
    *,
    case_id: int,
    actor_external_id: str,
    decision_code: str,
    decision_comment: str | None,
    comment: str | None,
    idempotency_key: str | None,
) -> dict:
    settings = get_settings()
    row = _get_case(session, case_id)
    existing = _get_existing_event_by_key(
        session, case_id, EVENT_DECISION_RECORDED, idempotency_key
    )
    if existing is not None:
        _sync_case_with_bitrix(session, case_id=case_id)
        return _serialize_case(row)
    _require_transition(row, expected_status=STATUS_UNDER_REVIEW, action_name="decision")
    normalized_decision_code = _validate_decision_code(decision_code)
    action_at = utcnow().replace(tzinfo=None)
    previous_status = row.current_status
    row.current_status = STATUS_DECISION_READY
    row.decision_code = normalized_decision_code
    row.decision_label = _decision_label_from_code(normalized_decision_code)
    row.decision_comment = _coalesce_text(decision_comment)
    row.due_at = _notify_due_at(anchor_at=action_at, settings=settings)
    _append_event(
        session,
        case_row=row,
        event_type=EVENT_DECISION_RECORDED,
        actor_external_id=actor_external_id,
        source="api",
        comment=comment,
        meta={
            "from_status": previous_status,
            "to_status": row.current_status,
            "decision_code": normalized_decision_code,
            "decision_comment": row.decision_comment,
        },
        idempotency_key=idempotency_key,
        event_at=action_at,
    )
    session.commit()
    _sync_case_with_bitrix(session, case_id=case_id)
    session.refresh(row)
    return _serialize_case(row)


def mark_client_notified(
    session: Session,
    *,
    case_id: int,
    actor_external_id: str,
    comment: str | None,
    idempotency_key: str | None,
) -> dict:
    row = _get_case(session, case_id)
    existing = _get_existing_event_by_key(session, case_id, EVENT_CLIENT_NOTIFIED, idempotency_key)
    if existing is not None:
        _sync_case_with_bitrix(session, case_id=case_id)
        return _serialize_case(row)
    _require_transition(row, expected_status=STATUS_DECISION_READY, action_name="client-notified")
    action_at = utcnow().replace(tzinfo=None)
    previous_status = row.current_status
    row.current_status = STATUS_CLIENT_NOTIFIED
    row.client_notified = True
    row.due_at = None
    _append_event(
        session,
        case_row=row,
        event_type=EVENT_CLIENT_NOTIFIED,
        actor_external_id=actor_external_id,
        source="api",
        comment=comment,
        meta={
            "from_status": previous_status,
            "to_status": row.current_status,
            "client_notified": True,
        },
        idempotency_key=idempotency_key,
        event_at=action_at,
    )
    session.commit()
    _sync_case_with_bitrix(session, case_id=case_id)
    session.refresh(row)
    return _serialize_case(row)


def complete_case(
    session: Session,
    *,
    case_id: int,
    actor_external_id: str,
    completion_outcome: str,
    comment: str | None,
    idempotency_key: str | None,
) -> dict:
    normalized_outcome = _validate_completion_outcome(completion_outcome)
    row = _get_case(session, case_id)
    event_type = (
        EVENT_RETURNED_TO_CENTRAL_DEFECT
        if normalized_outcome == STATUS_RETURNED_TO_CENTRAL_DEFECT
        else EVENT_RETURNED_TO_STORE
    )
    existing = _get_existing_event_by_key(session, case_id, event_type, idempotency_key)
    if existing is not None:
        _sync_case_with_bitrix(session, case_id=case_id)
        return _serialize_case(row)
    _require_transition(row, expected_status=STATUS_CLIENT_NOTIFIED, action_name="complete")
    if (
        row.decision_code == DECISION_APPROVED
        and normalized_outcome != STATUS_RETURNED_TO_CENTRAL_DEFECT
    ):
        raise _http_error(
            409,
            {
                "message": "invalid completion_outcome for approved decision",
                "current_status": row.current_status,
                "decision_code": row.decision_code,
                "expected_completion_outcome": STATUS_RETURNED_TO_CENTRAL_DEFECT,
            },
        )
    if row.decision_code == DECISION_REJECTED and normalized_outcome != STATUS_RETURNED_TO_STORE:
        raise _http_error(
            409,
            {
                "message": "invalid completion_outcome for rejected decision",
                "current_status": row.current_status,
                "decision_code": row.decision_code,
                "expected_completion_outcome": STATUS_RETURNED_TO_STORE,
            },
        )
    if row.decision_code not in DECISION_CODES:
        raise _http_error(
            409,
            {
                "message": "decision must be recorded before completion",
                "current_status": row.current_status,
                "decision_code": row.decision_code,
            },
        )
    action_at = utcnow().replace(tzinfo=None)
    previous_status = row.current_status
    row.current_status = normalized_outcome
    row.due_at = None
    _append_event(
        session,
        case_row=row,
        event_type=event_type,
        actor_external_id=actor_external_id,
        source="api",
        comment=comment,
        meta={
            "from_status": previous_status,
            "to_status": row.current_status,
            "completion_outcome": normalized_outcome,
        },
        idempotency_key=idempotency_key,
        event_at=action_at,
    )
    session.commit()
    _sync_case_with_bitrix(session, case_id=case_id)
    session.refresh(row)
    return _serialize_case(row)


def backfill_completion_outcomes(session: Session) -> dict[str, int]:
    rows = session.scalars(
        select(ExpertiseCase).where(
            ExpertiseCase.current_status == STATUS_RETURNED_TO_STORE,
            ExpertiseCase.decision_code == DECISION_APPROVED,
        )
    ).all()
    updated = 0
    for row in rows:
        row.current_status = STATUS_RETURNED_TO_CENTRAL_DEFECT
        row.due_at = None
        events = session.scalars(
            select(ExpertiseCaseEvent).where(
                ExpertiseCaseEvent.expertise_case_id == row.id,
                ExpertiseCaseEvent.event_type == EVENT_RETURNED_TO_STORE,
            )
        ).all()
        for event in events:
            event.event_type = EVENT_RETURNED_TO_CENTRAL_DEFECT
            if isinstance(event.meta, dict):
                event.meta = {
                    **event.meta,
                    "to_status": STATUS_RETURNED_TO_CENTRAL_DEFECT,
                    "completion_outcome": STATUS_RETURNED_TO_CENTRAL_DEFECT,
                }
        updated += 1
    session.commit()
    return {"updated": updated}
