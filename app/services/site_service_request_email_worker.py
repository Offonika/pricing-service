from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.site_service_requests import (
    SiteServiceRequestCase,
    SiteServiceRequestEvent,
    SiteServiceRequestSource,
)
from app.schemas.site_service_requests import SiteServiceEmailEventPayload
from app.services.site_service_requests import (
    SiteServiceRequestCipher,
    SiteServiceRequestConfigurationError,
)
from app.services.site_service_requests_worker import (
    SiteServiceRequestBitrixApi,
    SiteServiceRequestBitrixReader,
    SiteServiceRequestBitrixWriter,
    SiteServiceRequestPermanentError,
    SiteServiceRequestWorkerResult,
    _as_utc,
    _item_field_value,
    _positive_int,
    _record_site_service_request_failure,
    _site_service_request_enum_value,
    decide_site_service_assignment,
    resolved_site_service_request_field_map,
    validate_site_service_request_enum_map,
)

_CRM_EMAIL_PROVIDER = "CRM_EMAIL"
_INCOMING_DIRECTION = 1
_OUTGOING_DIRECTION = 2
_DEAL_OWNER_TYPE_ID = 2
_CONTACT_OWNER_TYPE_ID = 3
_EMAIL_FIELD_KEYS = {"mail_activity_id", "mail_activity_url", "mail_thread_key"}


@dataclass(frozen=True)
class EmailEventVerification:
    activity: dict[str, Any]
    deal: dict[str, Any]
    contact: dict[str, Any]
    deal_manager_user_id: int | None
    bindings: tuple[tuple[int, int], ...]


def process_site_service_email_events(
    session: Session,
    *,
    settings: Settings,
    api: SiteServiceRequestBitrixApi,
    cipher: SiteServiceRequestCipher,
    now: datetime | None = None,
    limit: int | None = None,
) -> list[SiteServiceRequestWorkerResult]:
    if not settings.site_service_requests_email_ingest_enabled:
        return []
    if not settings.site_service_requests_bitrix_writes_enabled:
        raise SiteServiceRequestConfigurationError(
            "site service request Bitrix writes are disabled"
        )
    field_map = resolved_site_service_email_field_map(settings)
    validate_site_service_request_enum_map(settings)
    stage_id = str(settings.site_service_requests_bitrix_stage_map.get("new") or "").strip()
    if not stage_id:
        raise SiteServiceRequestConfigurationError(
            "site service request Bitrix NEW stage is not configured"
        )

    current_time = _as_utc(now or datetime.now(UTC))
    batch_limit = limit or settings.site_service_requests_worker_batch_size
    available = or_(
        SiteServiceRequestEvent.status == "pending",
        and_(
            SiteServiceRequestEvent.status == "retry",
            or_(
                SiteServiceRequestEvent.next_retry_at.is_(None),
                SiteServiceRequestEvent.next_retry_at <= current_time,
            ),
        ),
    )
    first_per_case = (
        select(
            SiteServiceRequestEvent.case_id.label("case_id"),
            func.min(SiteServiceRequestEvent.source_message_id).label("source_message_id"),
        )
        .where(
            SiteServiceRequestEvent.status.in_(("pending", "retry")),
            SiteServiceRequestEvent.event_type.like("email.%"),
        )
        .group_by(SiteServiceRequestEvent.case_id)
        .subquery()
    )
    event_ids = session.scalars(
        select(SiteServiceRequestEvent.id)
        .join(
            first_per_case,
            and_(
                first_per_case.c.case_id == SiteServiceRequestEvent.case_id,
                first_per_case.c.source_message_id == SiteServiceRequestEvent.source_message_id,
            ),
        )
        .where(available, SiteServiceRequestEvent.event_type.like("email.%"))
        .order_by(SiteServiceRequestEvent.created_at, SiteServiceRequestEvent.id)
        .limit(batch_limit)
    ).all()

    reader = SiteServiceRequestBitrixReader(api)
    writer = SiteServiceRequestBitrixWriter(api)
    results: list[SiteServiceRequestWorkerResult] = []
    for event_id in event_ids:
        event_key = str(
            session.scalar(
                select(SiteServiceRequestEvent.event_id).where(
                    SiteServiceRequestEvent.id == event_id
                )
            )
            or event_id
        )
        try:
            result = _process_site_service_email_event(
                session,
                event_id=event_id,
                settings=settings,
                api=api,
                reader=reader,
                writer=writer,
                cipher=cipher,
                field_map=field_map,
                stage_id=stage_id,
                now=current_time,
            )
            session.commit()
        except SiteServiceRequestPermanentError as exc:
            session.rollback()
            result = _record_site_service_request_failure(
                session,
                event_id=event_key,
                error_code=exc.code,
                permanent=True,
                now=current_time,
            )
            session.commit()
        except (RuntimeError, SQLAlchemyError):
            session.rollback()
            result = _record_site_service_request_failure(
                session,
                event_id=event_key,
                error_code="bitrix_unavailable",
                permanent=False,
                now=current_time,
            )
            session.commit()
        results.append(result)
    return results


def resolved_site_service_email_field_map(settings: Settings) -> dict[str, str]:
    field_map = resolved_site_service_request_field_map(settings)
    missing = sorted(key for key in _EMAIL_FIELD_KEYS if not field_map.get(key))
    if missing:
        raise SiteServiceRequestConfigurationError(
            "site service request email field mapping is incomplete: " + ", ".join(missing)
        )
    return field_map


def _process_site_service_email_event(
    session: Session,
    *,
    event_id: int,
    settings: Settings,
    api: SiteServiceRequestBitrixApi,
    reader: SiteServiceRequestBitrixReader,
    writer: SiteServiceRequestBitrixWriter,
    cipher: SiteServiceRequestCipher,
    field_map: dict[str, str],
    stage_id: str,
    now: datetime,
) -> SiteServiceRequestWorkerResult:
    event = session.scalar(
        select(SiteServiceRequestEvent)
        .where(SiteServiceRequestEvent.id == event_id)
        .with_for_update()
    )
    if event is None:
        raise SiteServiceRequestPermanentError("event_payload_unavailable")
    if event.status not in {"pending", "retry"}:
        return SiteServiceRequestWorkerResult(
            event_id=event.event_id,
            status=event.status,
            bitrix_item_id=event.case.bitrix_item_id,
            error_code=event.last_error_code,
        )
    payload = _decrypt_email_payload(event, cipher=cipher)
    case = session.scalar(
        select(SiteServiceRequestCase)
        .where(SiteServiceRequestCase.id == event.case_id)
        .with_for_update()
    )
    if case is None:
        raise SiteServiceRequestPermanentError("case_not_found")
    source = session.scalar(
        select(SiteServiceRequestSource).where(
            SiteServiceRequestSource.case_id == case.id,
            SiteServiceRequestSource.source_kind == "bitrix_mail",
            SiteServiceRequestSource.source_key == payload.source_key,
        )
    )
    if source is None:
        raise SiteServiceRequestPermanentError("email_source_not_found")
    verification = _verify_email_event(
        payload=payload,
        api=api,
        settings=settings,
    )
    if payload.event_type == "email.replied":
        return _apply_email_reply(
            session,
            event=event,
            case=case,
            source=source,
            payload=payload,
            verification=verification,
            settings=settings,
            api=api,
            writer=writer,
            field_map=field_map,
            now=now,
        )

    last_assignment = session.scalar(
        select(SiteServiceRequestCase)
        .where(
            SiteServiceRequestCase.assigned_user_id.is_not(None),
            SiteServiceRequestCase.id != case.id,
        )
        .order_by(SiteServiceRequestCase.round_robin_seq.desc())
        .limit(1)
        .with_for_update()
    )
    max_round_robin_seq = int(
        session.scalar(select(func.max(SiteServiceRequestCase.round_robin_seq))) or 0
    )
    statuses = reader.timeman_statuses(settings.site_service_requests_first_line_user_ids)
    assignment = decide_site_service_assignment(
        case=case,
        configured_user_ids=settings.site_service_requests_first_line_user_ids,
        timeman_statuses=statuses,
        last_assigned_user_id=(
            last_assignment.assigned_user_id if last_assignment is not None else None
        ),
        next_round_robin_seq=max_round_robin_seq + 1,
        escalation_user_id=settings.site_service_requests_escalation_user_id,
        first_response_hours=settings.site_service_requests_first_response_hours,
        timezone_name=settings.site_service_requests_timezone,
        now=now,
    )
    case.crm_contact_id = payload.crm_contact_id
    case.crm_deal_id = payload.crm_deal_id
    case.deal_manager_user_id = verification.deal_manager_user_id
    case.assigned_user_id = assignment.assigned_user_id
    case.assignment_state = assignment.state
    case.intake_mode = assignment.intake_mode
    case.first_response_due_at = assignment.first_response_due_at
    case.sla_paused_at = assignment.sla_paused_at
    case.escalated_at = assignment.escalated_at
    case.round_robin_seq = assignment.round_robin_seq

    fields = _email_item_fields(
        payload=payload,
        case=case,
        field_map=field_map,
        settings=settings,
        now=now,
    )
    if case.bitrix_item_id is None:
        item_id = writer.sync_item(
            entity_type_id=settings.site_service_requests_bitrix_entity_type_id,
            idempotency_field=field_map["idempotency_key"],
            idempotency_key=payload.source_key,
            fields=fields,
            create_only_fields={
                "categoryId": settings.site_service_requests_bitrix_working_category_id,
                "stageId": stage_id,
            },
        )
    else:
        # A technically linked site card keeps its original site idempotency key.
        update_fields = dict(fields)
        update_fields.pop(field_map["idempotency_key"], None)
        writer.update_item_fields(
            entity_type_id=settings.site_service_requests_bitrix_entity_type_id,
            item_id=case.bitrix_item_id,
            fields=update_fields,
        )
        item_id = case.bitrix_item_id

    _update_inbound_activity(
        api=api,
        payload=payload,
        verification=verification,
        item_id=item_id,
        assignee_id=case.assigned_user_id,
        deadline=case.first_response_due_at,
        entity_type_id=settings.site_service_requests_bitrix_entity_type_id,
    )
    _verify_item_email_fields(
        writer=writer,
        item_id=item_id,
        payload=payload,
        settings=settings,
        field_map=field_map,
    )
    case.bitrix_item_id = item_id
    case.base_sync_status = (
        "assignment_waiting" if case.assignment_state == "waiting" else "synced"
    )
    case.base_error_code = (
        "assignment_waiting" if case.assignment_state == "waiting" else None
    )
    case.sync_status = case.base_sync_status
    case.last_error_code = case.base_error_code
    case.version += 1
    case.updated_at = now
    _notify_deal_manager_once(
        case=case,
        payload=payload,
        settings=settings,
        writer=writer,
        now=now,
    )
    _mark_email_event_processed(event, now=now)
    session.flush()
    return SiteServiceRequestWorkerResult(
        event_id=event.event_id,
        status="processed",
        bitrix_item_id=item_id,
        error_code=case.last_error_code,
    )


def _apply_email_reply(
    session: Session,
    *,
    event: SiteServiceRequestEvent,
    case: SiteServiceRequestCase,
    source: SiteServiceRequestSource,
    payload: SiteServiceEmailEventPayload,
    verification: EmailEventVerification,
    settings: Settings,
    api: SiteServiceRequestBitrixApi,
    writer: SiteServiceRequestBitrixWriter,
    field_map: dict[str, str],
    now: datetime,
) -> SiteServiceRequestWorkerResult:
    if case.bitrix_item_id is None:
        raise SiteServiceRequestPermanentError("email_reply_case_not_synced")
    response_at = min(_as_utc(payload.occurred_at), now)
    if case.first_response_at is None or response_at < _as_utc(case.first_response_at):
        case.first_response_at = response_at
    case.sla_paused_at = None
    case.latest_outbound_message_id = max(
        case.latest_outbound_message_id or 0,
        payload.message_id,
    )
    writer.update_item_fields(
        entity_type_id=settings.site_service_requests_bitrix_entity_type_id,
        item_id=case.bitrix_item_id,
        fields={
            field_map["first_response_at"]: case.first_response_at,
            field_map["site_last_sync_at"]: now,
            field_map["mail_thread_key"]: payload.source_key,
        },
    )
    inbound_activity_ids = set(
        session.scalars(
            select(SiteServiceRequestEvent.source_activity_id).where(
                SiteServiceRequestEvent.case_id == case.id,
                SiteServiceRequestEvent.event_type == "email.received",
                SiteServiceRequestEvent.source_activity_id.is_not(None),
            )
        ).all()
    )
    primary_activity_id = source.primary_activity_id or case.primary_activity_id
    if primary_activity_id is not None:
        inbound_activity_ids.add(primary_activity_id)
    if not inbound_activity_ids:
        raise SiteServiceRequestPermanentError("email_primary_activity_missing")
    for activity_id in sorted(inbound_activity_ids):
        _complete_inbound_activity(
            api=api,
            activity_id=activity_id,
            expected_thread_id=payload.thread_id,
        )
    readback = writer.get_item(
        entity_type_id=settings.site_service_requests_bitrix_entity_type_id,
        item_id=case.bitrix_item_id,
    )
    if not _item_field_value(readback, field_map["first_response_at"]):
        raise RuntimeError("bitrix_first_response_readback_failed")
    case.version += 1
    case.updated_at = now
    case.sync_status = "synced"
    case.last_error_code = None
    _mark_email_event_processed(event, now=now)
    session.flush()
    return SiteServiceRequestWorkerResult(
        event_id=event.event_id,
        status="processed",
        bitrix_item_id=case.bitrix_item_id,
    )


def _verify_email_event(
    *,
    payload: SiteServiceEmailEventPayload,
    api: SiteServiceRequestBitrixApi,
    settings: Settings,
) -> EmailEventVerification:
    activity = _activity_get(api, payload.activity_id)
    if str(activity.get("PROVIDER_ID") or activity.get("providerId") or "") != _CRM_EMAIL_PROVIDER:
        raise SiteServiceRequestPermanentError("email_activity_provider_mismatch")
    direction = _positive_int(activity.get("DIRECTION") or activity.get("direction"))
    expected_direction = (
        _INCOMING_DIRECTION if payload.event_type == "email.received" else _OUTGOING_DIRECTION
    )
    if direction != expected_direction:
        raise SiteServiceRequestPermanentError("email_activity_direction_mismatch")
    activity_id = _positive_int(activity.get("ID") or activity.get("id"))
    if activity_id != payload.activity_id:
        raise RuntimeError("email_activity_readback_failed")
    thread_id = _positive_int(activity.get("THREAD_ID") or activity.get("threadId"))
    if thread_id != payload.thread_id:
        raise SiteServiceRequestPermanentError("email_activity_thread_mismatch")

    bindings = _activity_bindings(activity)
    if (_DEAL_OWNER_TYPE_ID, payload.crm_deal_id) not in bindings:
        raise SiteServiceRequestPermanentError("email_activity_deal_binding_mismatch")
    if (_CONTACT_OWNER_TYPE_ID, payload.crm_contact_id) not in bindings:
        raise SiteServiceRequestPermanentError("email_activity_contact_binding_mismatch")
    if payload.existing_service_item_id is not None and (
        settings.site_service_requests_bitrix_entity_type_id,
        payload.existing_service_item_id,
    ) not in bindings:
        raise SiteServiceRequestPermanentError("email_service_binding_mismatch")

    deal_response = api.call("crm.deal.get", [("id", str(payload.crm_deal_id))])
    deal = deal_response.get("result")
    if not isinstance(deal, dict):
        raise RuntimeError("email_deal_readback_failed")
    deal_id = _positive_int(deal.get("ID") or deal.get("id"))
    contact_id = _positive_int(deal.get("CONTACT_ID") or deal.get("contactId"))
    if deal_id != payload.crm_deal_id or contact_id != payload.crm_contact_id:
        raise SiteServiceRequestPermanentError("email_deal_contact_mismatch")
    order_field = str(settings.site_service_requests_crm_order_field or "").strip()
    if not order_field:
        raise SiteServiceRequestConfigurationError(
            "site service request CRM order field is not configured"
        )
    if str(deal.get(order_field) or "").strip() != payload.order_number:
        raise SiteServiceRequestPermanentError("email_order_mismatch")

    contact_response = api.call("crm.contact.get", [("id", str(payload.crm_contact_id))])
    contact = contact_response.get("result")
    if not isinstance(contact, dict):
        raise RuntimeError("email_contact_readback_failed")
    if _positive_int(contact.get("ID") or contact.get("id")) != payload.crm_contact_id:
        raise RuntimeError("email_contact_readback_failed")
    contact_emails = _contact_emails(contact)
    communication_emails = _activity_communication_emails(
        activity,
        contact_id=payload.crm_contact_id,
    )
    if len(communication_emails) != 1 or communication_emails[0] not in contact_emails:
        raise SiteServiceRequestPermanentError("email_sender_contact_mismatch")

    return EmailEventVerification(
        activity=activity,
        deal=deal,
        contact=contact,
        deal_manager_user_id=_positive_int(
            deal.get("ASSIGNED_BY_ID") or deal.get("assignedById")
        ),
        bindings=bindings,
    )


def _email_item_fields(
    *,
    payload: SiteServiceEmailEventPayload,
    case: SiteServiceRequestCase,
    field_map: dict[str, str],
    settings: Settings,
    now: datetime,
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "title": f"Email-обращение по заказу №{payload.order_number}"[:255],
        field_map["source"]: f"bitrix-mail:{payload.mailbox}",
        field_map["crm_contact"]: payload.crm_contact_id,
        field_map["crm_deal"]: payload.crm_deal_id,
        field_map["order_refs"]: payload.order_number,
        field_map["problem_description"]: (
            "Обращение получено по email. Ответить в исходной CRM email-цепочке."
        ),
        field_map["request_type"]: _site_service_request_enum_value(
            settings,
            f"request_type_{payload.request_type}",
        ),
        field_map["backend_case_id"]: case.id,
        field_map["idempotency_key"]: payload.source_key,
        field_map["site_sync_status"]: _site_service_request_enum_value(
            settings,
            (
                "sync_status_assignment_waiting"
                if case.assignment_state == "waiting"
                else "sync_status_synced"
            ),
        ),
        field_map["site_last_sync_at"]: now,
        field_map["first_response_due_at"]: case.first_response_due_at,
        field_map["first_response_at"]: case.first_response_at,
        field_map["site_sync_error"]: (
            "assignment_waiting" if case.assignment_state == "waiting" else None
        ),
        field_map["mail_activity_id"]: str(payload.activity_id),
        field_map["mail_activity_url"]: _activity_url(settings, payload.activity_id),
        field_map["mail_thread_key"]: payload.source_key,
    }
    if case.assigned_user_id is not None or case.assignment_state == "waiting":
        fields["assignedById"] = case.assigned_user_id
    # Explicitly clear values only for fields that support it; omitted values do
    # not disturb a technically linked site ticket.
    return {key: value for key, value in fields.items() if value is not None}


def _update_inbound_activity(
    *,
    api: SiteServiceRequestBitrixApi,
    payload: SiteServiceEmailEventPayload,
    verification: EmailEventVerification,
    item_id: int,
    assignee_id: int | None,
    deadline: datetime | None,
    entity_type_id: int,
) -> None:
    bindings = list(verification.bindings)
    dynamic_binding = (entity_type_id, item_id)
    if dynamic_binding not in bindings:
        bindings.append(dynamic_binding)
    fields: dict[str, Any] = {
        "COMPLETED": "N",
        "BINDINGS": [
            {"OWNER_TYPE_ID": owner_type_id, "OWNER_ID": owner_id}
            for owner_type_id, owner_id in bindings
        ],
    }
    if assignee_id is not None:
        fields["RESPONSIBLE_ID"] = assignee_id
    if deadline is not None:
        fields["DEADLINE"] = deadline.isoformat()
    _activity_update(api, payload.activity_id, fields)
    readback = _activity_get(api, payload.activity_id)
    if str(readback.get("COMPLETED") or readback.get("completed") or "N") != "N":
        raise RuntimeError("email_activity_open_readback_failed")
    if dynamic_binding not in _activity_bindings(readback):
        raise RuntimeError("email_activity_service_binding_readback_failed")
    if assignee_id is not None and _positive_int(
        readback.get("RESPONSIBLE_ID") or readback.get("responsibleId")
    ) != assignee_id:
        raise RuntimeError("email_activity_assignment_readback_failed")


def _complete_inbound_activity(
    *,
    api: SiteServiceRequestBitrixApi,
    activity_id: int,
    expected_thread_id: int,
) -> None:
    before = _activity_get(api, activity_id)
    if str(before.get("PROVIDER_ID") or before.get("providerId") or "") != _CRM_EMAIL_PROVIDER:
        raise SiteServiceRequestPermanentError("email_primary_activity_provider_mismatch")
    if _positive_int(before.get("DIRECTION") or before.get("direction")) != _INCOMING_DIRECTION:
        raise SiteServiceRequestPermanentError("email_primary_activity_direction_mismatch")
    if _positive_int(before.get("THREAD_ID") or before.get("threadId")) != expected_thread_id:
        raise SiteServiceRequestPermanentError("email_primary_activity_thread_mismatch")
    if str(before.get("COMPLETED") or before.get("completed") or "N") != "Y":
        _activity_update(api, activity_id, {"COMPLETED": "Y"})
    readback = _activity_get(api, activity_id)
    if str(readback.get("COMPLETED") or readback.get("completed") or "N") != "Y":
        raise RuntimeError("email_activity_completion_readback_failed")


def _verify_item_email_fields(
    *,
    writer: SiteServiceRequestBitrixWriter,
    item_id: int,
    payload: SiteServiceEmailEventPayload,
    settings: Settings,
    field_map: dict[str, str],
) -> None:
    item = writer.get_item(
        entity_type_id=settings.site_service_requests_bitrix_entity_type_id,
        item_id=item_id,
    )
    if str(_item_field_value(item, field_map["mail_activity_id"]) or "") != str(
        payload.activity_id
    ):
        raise RuntimeError("email_item_activity_readback_failed")
    if str(_item_field_value(item, field_map["mail_thread_key"]) or "") != payload.source_key:
        raise RuntimeError("email_item_thread_readback_failed")


def _notify_deal_manager_once(
    *,
    case: SiteServiceRequestCase,
    payload: SiteServiceEmailEventPayload,
    settings: Settings,
    writer: SiteServiceRequestBitrixWriter,
    now: datetime,
) -> None:
    if case.deal_manager_notified_at is not None or case.deal_manager_user_id is None:
        return
    if case.bitrix_item_id is None:
        raise RuntimeError("email_item_missing_for_notification")
    writer.notify_user(
        user_id=case.deal_manager_user_id,
        message=(
            "Новое сервисное email-обращение по заказу "
            f"№{payload.order_number}. Ответственный назначается сервисной очередью. "
            f"[URL={_item_url(settings, case.bitrix_item_id)}]Открыть обращение[/URL]"
        ),
        tag=f"mm-service-email-manager:{case.id}",
    )
    case.deal_manager_notified_at = now


def _decrypt_email_payload(
    event: SiteServiceRequestEvent,
    *,
    cipher: SiteServiceRequestCipher,
) -> SiteServiceEmailEventPayload:
    if event.payload_encrypted is None:
        raise SiteServiceRequestPermanentError("event_payload_unavailable")
    try:
        return SiteServiceEmailEventPayload.model_validate_json(
            cipher.decrypt(event.payload_encrypted, event_id=event.event_id)
        )
    except (ValidationError, SiteServiceRequestConfigurationError) as exc:
        raise SiteServiceRequestPermanentError("event_payload_invalid") from exc


def _activity_get(api: SiteServiceRequestBitrixApi, activity_id: int) -> dict[str, Any]:
    response = api.call("crm.activity.get", [("id", str(activity_id))])
    result = response.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("email_activity_readback_failed")
    return result


def _activity_update(
    api: SiteServiceRequestBitrixApi,
    activity_id: int,
    fields: dict[str, Any],
) -> None:
    response = api.call_json(
        "crm.activity.update",
        {"id": activity_id, "fields": fields},
    )
    if response.get("result") not in (True, 1, "1"):
        raise RuntimeError("email_activity_write_failed")


def _activity_bindings(activity: dict[str, Any]) -> tuple[tuple[int, int], ...]:
    raw = activity.get("BINDINGS") or activity.get("bindings") or []
    if not isinstance(raw, list):
        raise RuntimeError("email_activity_bindings_invalid")
    bindings: list[tuple[int, int]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise RuntimeError("email_activity_bindings_invalid")
        owner_type_id = _positive_int(
            item.get("OWNER_TYPE_ID") or item.get("ownerTypeId")
        )
        owner_id = _positive_int(item.get("OWNER_ID") or item.get("ownerId"))
        if owner_type_id is None or owner_id is None:
            raise RuntimeError("email_activity_bindings_invalid")
        binding = (owner_type_id, owner_id)
        if binding not in bindings:
            bindings.append(binding)
    return tuple(bindings)


def _contact_emails(contact: dict[str, Any]) -> set[str]:
    raw = contact.get("EMAIL") or contact.get("email") or []
    if not isinstance(raw, list):
        raise RuntimeError("email_contact_readback_failed")
    result = {
        str(item.get("VALUE") or item.get("value") or "").strip().casefold()
        for item in raw
        if isinstance(item, dict)
    }
    result.discard("")
    if not result:
        raise SiteServiceRequestPermanentError("email_contact_has_no_email")
    return result


def _activity_communication_emails(
    activity: dict[str, Any],
    *,
    contact_id: int,
) -> tuple[str, ...]:
    raw = activity.get("COMMUNICATIONS") or activity.get("communications") or []
    if not isinstance(raw, list):
        raise RuntimeError("email_activity_communications_invalid")
    values: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise RuntimeError("email_activity_communications_invalid")
        entity_id = _positive_int(item.get("ENTITY_ID") or item.get("entityId"))
        entity_type_id = _positive_int(
            item.get("ENTITY_TYPE_ID") or item.get("entityTypeId")
        )
        if entity_id != contact_id or entity_type_id != _CONTACT_OWNER_TYPE_ID:
            continue
        value = str(item.get("VALUE") or item.get("value") or "").strip().casefold()
        if value:
            values.add(value)
    return tuple(sorted(values))


def _portal_base_url(settings: Settings) -> str:
    webhook = str(settings.site_service_requests_bitrix_webhook_url or "")
    parsed = urlsplit(webhook)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SiteServiceRequestConfigurationError(
            "site service request Bitrix webhook URL is invalid"
        )
    return f"{parsed.scheme}://{parsed.netloc}"


def _activity_url(settings: Settings, activity_id: int) -> str:
    return f"{_portal_base_url(settings)}/crm/activity/?ID={activity_id}&open_view={activity_id}"


def _item_url(settings: Settings, item_id: int) -> str:
    return (
        f"{_portal_base_url(settings)}/crm/type/"
        f"{settings.site_service_requests_bitrix_entity_type_id}/details/{item_id}/"
    )


def _mark_email_event_processed(event: SiteServiceRequestEvent, *, now: datetime) -> None:
    event.status = "processed"
    event.attempts += 1
    event.next_retry_at = None
    event.last_error_code = None
    event.consecutive_permanent_failures = 0
    event.processed_at = now
    event.updated_at = now
    event.payload_encrypted = None
