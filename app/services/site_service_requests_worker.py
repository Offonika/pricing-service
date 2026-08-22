from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.site_service_requests import (
    SiteServiceRequestCase,
    SiteServiceRequestCommand,
    SiteServiceRequestEvent,
    SiteServiceRequestFile,
)
from app.schemas.site_service_requests import SiteServiceRequestEventPayload
from app.services.site_service_requests import (
    SiteServiceRequestCipher,
    SiteServiceRequestConfigurationError,
)

_RETRY_DELAYS_SECONDS = (60, 120, 300, 900, 1800)
_DEFAULT_FIELD_MAP = {
    "source": "UF_CRM_36_SOURCE",
    "customer_contact": "UF_CRM_36_CUSTOMERCONTACT",
    "crm_contact": "UF_CRM_36_CRMCONTACT",
    "crm_company": "UF_CRM_36_CRMCOMPANY",
    "crm_deal": "UF_CRM_36_CRMDEAL",
    "order_refs": "UF_CRM_36_ORDERREFS",
    "problem_description": "UF_CRM_36_PROBLEMDESCRIPTION",
    "request_type": "UF_CRM_36_CUSTOMERREQUESTCHOICE",
    "files": "UF_CRM_36_CLIENTFILES",
    "backend_case_id": "UF_CRM_36_BACKENDCASEID",
    "idempotency_key": "UF_CRM_36_IDEMPOTENCYKEY",
}
_REQUIRED_WORKER_FIELD_KEYS = {
    "site_ticket_id",
    "site_ticket_url",
    "site_history",
    "site_sync_status",
    "site_last_sync_at",
    "first_response_due_at",
    "first_response_at",
    "site_sync_error",
}


class SiteServiceRequestBitrixApi(Protocol):
    def call(
        self,
        method: str,
        params: list[tuple[str, str]] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]: ...

    def call_json(
        self,
        method: str,
        payload: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ContactMatch:
    status: str
    contact_id: int | None = None
    company_id: int | None = None
    candidate_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class OrderMatch:
    status: str
    deal_id: int | None = None
    candidate_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class AssignmentDecision:
    state: str
    assigned_user_id: int | None
    intake_mode: str
    first_response_due_at: datetime | None
    sla_paused_at: datetime | None
    escalated_at: datetime | None
    round_robin_seq: int


@dataclass(frozen=True)
class SiteServiceRequestWorkerPlan:
    event_id: str
    case_id: int
    ticket_id: int
    contact_status: str
    contact_id: int | None
    company_id: int | None
    order_status: str
    deal_id: int | None
    assignment_state: str
    assigned_user_id: int | None
    intake_mode: str
    first_response_due_at: datetime | None
    sla_paused_at: datetime | None
    escalated_at: datetime | None
    round_robin_seq: int
    escalated: bool
    actions: tuple[str, ...]


@dataclass(frozen=True)
class SiteServiceRequestWorkerResult:
    event_id: str
    status: str
    bitrix_item_id: int | None
    error_code: str | None = None


class SiteServiceRequestPermanentError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class SiteServiceRequestBitrixReader:
    def __init__(self, api: SiteServiceRequestBitrixApi):
        self.api = api

    def find_contact(self, *, phone: str, email: str | None) -> ContactMatch:
        normalized_phone = normalize_site_service_phone(phone)
        normalized_email = normalize_site_service_email(email)
        phone_ids = self._duplicate_contact_ids("PHONE", normalized_phone)
        candidate_ids = phone_ids
        if not candidate_ids and normalized_email:
            candidate_ids = self._duplicate_contact_ids("EMAIL", normalized_email)

        active_contacts: list[tuple[int, int | None]] = []
        for contact_id in candidate_ids:
            response = self.api.call("crm.contact.get", [("id", str(contact_id))])
            contact = response.get("result") or {}
            if not isinstance(contact, dict) or str(contact.get("ACTIVE", "Y")).upper() == "N":
                continue
            company_id = _positive_int(contact.get("COMPANY_ID"))
            active_contacts.append((contact_id, company_id))

        if not active_contacts:
            return ContactMatch(status="not_found")
        if len(active_contacts) > 1:
            return ContactMatch(
                status="ambiguous",
                candidate_ids=tuple(item[0] for item in active_contacts),
            )
        contact_id, company_id = active_contacts[0]
        return ContactMatch(
            status="matched",
            contact_id=contact_id,
            company_id=company_id,
            candidate_ids=(contact_id,),
        )

    def find_order(
        self,
        *,
        contact_id: int | None,
        order_number: str | None,
        order_field: str | None,
    ) -> OrderMatch:
        normalized_order = (order_number or "").strip()
        if contact_id is None or not normalized_order:
            return OrderMatch(status="not_found")

        if order_field:
            exact_response = self.api.call(
                "crm.deal.list",
                [
                    ("filter[CONTACT_ID]", str(contact_id)),
                    (f"filter[={order_field}]", normalized_order),
                    ("select[]", "ID"),
                    ("select[]", "TITLE"),
                ],
            )
            exact_ids = _deal_ids(exact_response)
            if len(exact_ids) == 1:
                return OrderMatch(
                    status="matched",
                    deal_id=exact_ids[0],
                    candidate_ids=tuple(exact_ids),
                )
            if len(exact_ids) > 1:
                return OrderMatch(status="ambiguous", candidate_ids=tuple(exact_ids))

        fallback_response = self.api.call(
            "crm.deal.list",
            [
                ("filter[CONTACT_ID]", str(contact_id)),
                ("select[]", "ID"),
                ("select[]", "TITLE"),
            ],
        )
        fallback_ids = [
            deal_id
            for deal_id, title in _deal_id_titles(fallback_response)
            if contains_exact_order_token(title, normalized_order)
        ]
        if len(fallback_ids) == 1:
            return OrderMatch(
                status="matched",
                deal_id=fallback_ids[0],
                candidate_ids=tuple(fallback_ids),
            )
        if len(fallback_ids) > 1:
            return OrderMatch(status="ambiguous", candidate_ids=tuple(fallback_ids))
        return OrderMatch(status="not_found")

    def timeman_statuses(self, user_ids: list[int]) -> dict[int, str]:
        statuses: dict[int, str] = {}
        for user_id in user_ids:
            response = self.api.call("timeman.status", [("USER_ID", str(user_id))])
            result = response.get("result") or {}
            status = result.get("STATUS") if isinstance(result, dict) else None
            statuses[user_id] = str(status or "ERROR").upper()
        return statuses

    def _duplicate_contact_ids(self, comm_type: str, value: str | None) -> list[int]:
        if not value:
            return []
        response = self.api.call(
            "crm.duplicate.findbycomm",
            [("type", comm_type), ("values[]", value)],
        )
        result = response.get("result") or {}
        raw_ids = result.get("CONTACT") if isinstance(result, dict) else []
        return sorted({_positive_int(value) for value in raw_ids or []} - {None})


class SiteServiceRequestBitrixWriter:
    def __init__(self, api: SiteServiceRequestBitrixApi):
        self.api = api

    def create_contact(self, payload: SiteServiceRequestEventPayload) -> int:
        params = [
            ("fields[NAME]", f"Клиент сайта #{payload.ticket.id}"),
            ("fields[ORIGINATOR_ID]", "site-service-request"),
            ("fields[ORIGIN_ID]", f"site-support-ticket:{payload.ticket.id}"),
            ("fields[SOURCE_DESCRIPTION]", "site-service-request"),
            ("fields[PHONE][0][VALUE]", payload.ticket.phone),
            ("fields[PHONE][0][VALUE_TYPE]", "WORK"),
        ]
        if payload.ticket.email:
            params.extend(
                [
                    ("fields[EMAIL][0][VALUE]", payload.ticket.email),
                    ("fields[EMAIL][0][VALUE_TYPE]", "WORK"),
                ]
            )
        response = self.api.call("crm.contact.add", params)
        contact_id = _positive_int(response.get("result"))
        if contact_id is None:
            raise RuntimeError("crm_contact_write_failed")
        return contact_id

    def get_item(self, *, entity_type_id: int, item_id: int) -> dict[str, Any]:
        return self._readback_item(entity_type_id=entity_type_id, item_id=item_id)

    def update_item_fields(
        self,
        *,
        entity_type_id: int,
        item_id: int,
        fields: dict[str, Any],
    ) -> dict[str, Any]:
        self._update_item(entity_type_id=entity_type_id, item_id=item_id, fields=fields)
        return self._readback_item(entity_type_id=entity_type_id, item_id=item_id)

    def add_timeline_comment(
        self,
        *,
        entity_type_id: int,
        item_id: int,
        comment: str,
    ) -> None:
        self.api.call(
            "crm.timeline.comment.add",
            [
                ("fields[ENTITY_TYPE_ID]", str(entity_type_id)),
                ("fields[ENTITY_ID]", str(item_id)),
                ("fields[COMMENT]", comment),
            ],
        )

    def notify_user(self, *, user_id: int, message: str) -> None:
        response = self.api.call(
            "im.notify.personal.add",
            [("USER_ID", str(user_id)), ("MESSAGE", message)],
        )
        if not response.get("result"):
            raise RuntimeError("bitrix_notification_failed")

    def upload_file(
        self,
        *,
        folder_id: int,
        deterministic_name: str,
        content: bytes,
    ) -> tuple[str, str | None]:
        existing = self.api.call(
            "disk.folder.getchildren",
            [
                ("id", str(folder_id)),
                ("filter[NAME]", deterministic_name),
            ],
        )
        existing_file = _disk_file_from_payload(existing)
        if existing_file is not None:
            return existing_file
        try:
            response = self.api.call_json(
                "disk.folder.uploadfile",
                {
                    "id": str(folder_id),
                    "data": {"NAME": deterministic_name},
                    "fileContent": [
                        deterministic_name,
                        base64.b64encode(content).decode("ascii"),
                    ],
                    "generateUniqueName": False,
                },
            )
        except RuntimeError:
            readback = self.api.call(
                "disk.folder.getchildren",
                [
                    ("id", str(folder_id)),
                    ("filter[NAME]", deterministic_name),
                ],
            )
            existing_file = _disk_file_from_payload(readback)
            if existing_file is None:
                raise
            return existing_file
        uploaded = _disk_file_from_payload(response)
        if uploaded is None:
            raise RuntimeError("bitrix_file_upload_failed")
        return uploaded

    def sync_item(
        self,
        *,
        entity_type_id: int,
        idempotency_field: str,
        idempotency_key: str,
        fields: dict[str, Any],
        create_only_fields: dict[str, Any] | None = None,
        preferred_item_id: int | None = None,
    ) -> int:
        if preferred_item_id is not None:
            self._update_item(
                entity_type_id=entity_type_id,
                item_id=preferred_item_id,
                fields=fields,
            )
            self._readback_item(
                entity_type_id=entity_type_id,
                item_id=preferred_item_id,
            )
            return preferred_item_id
        existing = self._find_items(
            entity_type_id=entity_type_id,
            idempotency_field=idempotency_field,
            idempotency_key=idempotency_key,
        )
        if len(existing) > 1:
            raise SiteServiceRequestPermanentError("bitrix_item_ambiguous")
        if existing:
            item_id = existing[0]
            self._update_item(entity_type_id=entity_type_id, item_id=item_id, fields=fields)
            self._readback_item(entity_type_id=entity_type_id, item_id=item_id)
            return item_id

        try:
            item_id = self._add_item(
                entity_type_id=entity_type_id,
                fields={**fields, **(create_only_fields or {})},
            )
        except RuntimeError:
            readback = self._find_items(
                entity_type_id=entity_type_id,
                idempotency_field=idempotency_field,
                idempotency_key=idempotency_key,
            )
            if len(readback) != 1:
                raise
            item_id = readback[0]
        self._readback_item(entity_type_id=entity_type_id, item_id=item_id)
        return item_id

    def _find_items(
        self,
        *,
        entity_type_id: int,
        idempotency_field: str,
        idempotency_key: str,
    ) -> list[int]:
        response = self.api.call(
            "crm.item.list",
            [
                ("entityTypeId", str(entity_type_id)),
                (
                    f"filter[{_bitrix_item_field_name(idempotency_field)}]",
                    idempotency_key,
                ),
                ("select[]", "id"),
            ],
        )
        result = response.get("result") or {}
        items = result.get("items") if isinstance(result, dict) else []
        return sorted(
            {
                item_id
                for item in items or []
                if isinstance(item, dict)
                for item_id in [_positive_int(item.get("id") or item.get("ID"))]
                if item_id is not None
            }
        )

    def _add_item(self, *, entity_type_id: int, fields: dict[str, Any]) -> int:
        response = self.api.call(
            "crm.item.add",
            _item_params(entity_type_id=entity_type_id, fields=fields),
        )
        result = response.get("result") or {}
        item = result.get("item") if isinstance(result, dict) else None
        item_id = _positive_int(item.get("id") if isinstance(item, dict) else result)
        if item_id is None:
            raise RuntimeError("bitrix_item_write_failed")
        return item_id

    def _update_item(
        self,
        *,
        entity_type_id: int,
        item_id: int,
        fields: dict[str, Any],
    ) -> None:
        self.api.call(
            "crm.item.update",
            [
                ("entityTypeId", str(entity_type_id)),
                ("id", str(item_id)),
                *_field_params(fields),
            ],
        )

    def _readback_item(self, *, entity_type_id: int, item_id: int) -> dict[str, Any]:
        response = self.api.call(
            "crm.item.get",
            [("entityTypeId", str(entity_type_id)), ("id", str(item_id))],
        )
        result = response.get("result") or {}
        item = result.get("item") if isinstance(result, dict) else None
        if not isinstance(item, dict) or _positive_int(item.get("id") or item.get("ID")) != item_id:
            raise RuntimeError("bitrix_item_readback_failed")
        return item


def normalize_site_service_phone(value: str | None) -> str | None:
    digits = re.sub(r"\D+", "", value or "")
    if len(digits) == 10:
        digits = "7" + digits
    elif len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return digits if len(digits) >= 11 else None


def normalize_site_service_email(value: str | None) -> str | None:
    normalized = (value or "").strip().lower()
    return normalized or None


def contains_exact_order_token(title: str | None, order_number: str) -> bool:
    normalized_order = order_number.strip()
    if not normalized_order:
        return False
    if normalized_order.isdigit():
        pattern = rf"(?<!\d){re.escape(normalized_order)}(?!\d)"
    else:
        pattern = rf"(?<!\w){re.escape(normalized_order)}(?!\w)"
    return re.search(pattern, title or "", flags=re.IGNORECASE) is not None


def decide_site_service_assignment(
    *,
    case: SiteServiceRequestCase,
    configured_user_ids: list[int],
    timeman_statuses: dict[int, str],
    last_assigned_user_id: int | None,
    next_round_robin_seq: int,
    escalation_user_id: int | None,
    first_response_hours: int,
    timezone_name: str,
    now: datetime | None = None,
) -> AssignmentDecision:
    current_time = _as_utc(now or datetime.now(UTC))
    available = [
        user_id
        for user_id in configured_user_ids
        if timeman_statuses.get(user_id, "ERROR").upper() == "OPENED"
    ]
    intake_mode = case.intake_mode or ("during_open_shift" if available else "outside_open_shift")
    assigned_user_id = case.assigned_user_id
    assignment_state = case.assignment_state
    due_at = _as_utc(case.first_response_due_at) if case.first_response_due_at else None
    paused_at = _as_utc(case.sla_paused_at) if case.sla_paused_at else None
    escalated_at = _as_utc(case.escalated_at) if case.escalated_at else None
    round_robin_seq = case.round_robin_seq

    if available:
        if assigned_user_id is None:
            assigned_user_id = choose_site_service_assignee(
                configured_user_ids=configured_user_ids,
                available_user_ids=available,
                last_assigned_user_id=last_assigned_user_id,
            )
            assignment_state = "assigned"
            round_robin_seq = next_round_robin_seq
        if due_at is None:
            if intake_mode == "during_open_shift":
                due_at = _as_utc(case.first_seen_at) + timedelta(hours=first_response_hours)
            else:
                local_now = current_time.astimezone(ZoneInfo(timezone_name))
                due_at = local_now.replace(hour=12, minute=0, second=0, microsecond=0).astimezone(
                    UTC
                )
        if paused_at is not None and due_at is not None:
            due_at += current_time - paused_at
            paused_at = None
    else:
        if assigned_user_id is None:
            assignment_state = "waiting"
        if due_at is not None and case.first_response_at is None and paused_at is None:
            paused_at = current_time

    if (
        escalation_user_id is not None
        and case.first_response_at is None
        and due_at is not None
        and paused_at is None
        and due_at <= current_time
        and escalated_at is None
    ):
        assigned_user_id = escalation_user_id
        assignment_state = "escalated"
        escalated_at = current_time

    return AssignmentDecision(
        state=assignment_state,
        assigned_user_id=assigned_user_id,
        intake_mode=intake_mode,
        first_response_due_at=due_at,
        sla_paused_at=paused_at,
        escalated_at=escalated_at,
        round_robin_seq=round_robin_seq,
    )


def choose_site_service_assignee(
    *,
    configured_user_ids: list[int],
    available_user_ids: list[int],
    last_assigned_user_id: int | None,
) -> int | None:
    available = [user_id for user_id in configured_user_ids if user_id in available_user_ids]
    if not available:
        return None
    if len(available) == 1 or last_assigned_user_id not in available:
        return available[0]
    current_index = available.index(last_assigned_user_id)
    return available[(current_index + 1) % len(available)]


def build_site_service_request_worker_plans(
    session: Session,
    *,
    settings: Settings,
    reader: SiteServiceRequestBitrixReader,
    cipher: SiteServiceRequestCipher,
    now: datetime | None = None,
    limit: int | None = None,
    failure_results: list[SiteServiceRequestWorkerResult] | None = None,
    failure_writer: SiteServiceRequestBitrixWriter | None = None,
) -> list[SiteServiceRequestWorkerPlan]:
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
    first_event_per_case = (
        select(func.min(SiteServiceRequestEvent.id).label("event_id"))
        .where(available)
        .group_by(SiteServiceRequestEvent.case_id)
        .subquery()
    )
    event_ids = session.scalars(
        select(SiteServiceRequestEvent.id)
        .join(first_event_per_case, first_event_per_case.c.event_id == SiteServiceRequestEvent.id)
        .where(
            available,
        )
        .order_by(SiteServiceRequestEvent.created_at, SiteServiceRequestEvent.id)
        .limit(batch_limit)
    ).all()
    last_assignment = session.scalar(
        select(SiteServiceRequestCase)
        .where(SiteServiceRequestCase.assigned_user_id.is_not(None))
        .order_by(SiteServiceRequestCase.round_robin_seq.desc())
        .limit(1)
    )
    max_round_robin_seq = int(
        session.scalar(select(func.max(SiteServiceRequestCase.round_robin_seq))) or 0
    )
    virtual_last_assigned_user_id = (
        last_assignment.assigned_user_id if last_assignment is not None else None
    )
    next_round_robin_seq = max_round_robin_seq + 1
    plans: list[SiteServiceRequestWorkerPlan] = []
    for event_id in event_ids:
        try:
            event = session.scalar(
                select(SiteServiceRequestEvent)
                .where(SiteServiceRequestEvent.id == event_id)
                .with_for_update(skip_locked=True)
            )
            if event is None:
                continue
            payload = _decrypt_site_service_request_payload(event, cipher=cipher)
            case = event.case
            if case.crm_contact_id is not None:
                contact = ContactMatch(
                    status="matched",
                    contact_id=case.crm_contact_id,
                    company_id=case.crm_company_id,
                    candidate_ids=(case.crm_contact_id,),
                )
            else:
                contact = reader.find_contact(
                    phone=payload.ticket.phone,
                    email=payload.ticket.email,
                )
            order = reader.find_order(
                contact_id=contact.contact_id,
                order_number=payload.ticket.order_number,
                order_field=settings.site_service_requests_crm_order_field,
            )
            statuses = reader.timeman_statuses(settings.site_service_requests_first_line_user_ids)
            assignment = decide_site_service_assignment(
                case=case,
                configured_user_ids=settings.site_service_requests_first_line_user_ids,
                timeman_statuses=statuses,
                last_assigned_user_id=virtual_last_assigned_user_id,
                next_round_robin_seq=next_round_robin_seq,
                escalation_user_id=settings.site_service_requests_escalation_user_id,
                first_response_hours=settings.site_service_requests_first_response_hours,
                timezone_name=settings.site_service_requests_timezone,
                now=current_time,
            )
            if case.assigned_user_id is None and assignment.assigned_user_id is not None:
                virtual_last_assigned_user_id = assignment.assigned_user_id
                next_round_robin_seq += 1
            actions = ["sync_bitrix_item"]
            if contact.status == "not_found":
                actions.insert(0, "create_service_contact")
            elif contact.status == "ambiguous":
                actions.insert(0, "request_manual_contact_match")
            if order.status == "ambiguous":
                actions.append("request_manual_order_match")
            elif order.status == "not_found" and payload.ticket.order_number:
                actions.append("mark_order_not_found")
            if assignment.escalated_at and case.escalated_at is None:
                actions.append("escalate_once")
            plans.append(
                SiteServiceRequestWorkerPlan(
                    event_id=event.event_id,
                    case_id=event.case_id,
                    ticket_id=payload.ticket.id,
                    contact_status=contact.status,
                    contact_id=contact.contact_id,
                    company_id=contact.company_id,
                    order_status=order.status,
                    deal_id=order.deal_id,
                    assignment_state=assignment.state,
                    assigned_user_id=assignment.assigned_user_id,
                    intake_mode=assignment.intake_mode,
                    first_response_due_at=assignment.first_response_due_at,
                    sla_paused_at=assignment.sla_paused_at,
                    escalated_at=assignment.escalated_at,
                    round_robin_seq=assignment.round_robin_seq,
                    escalated=assignment.escalated_at is not None,
                    actions=tuple(actions),
                )
            )
        except SiteServiceRequestPermanentError as exc:
            if failure_results is None:
                raise
            session.rollback()
            failure = _record_site_service_request_failure(
                session,
                event_id=str(
                    session.scalar(
                        select(SiteServiceRequestEvent.event_id).where(
                            SiteServiceRequestEvent.id == event_id
                        )
                    )
                    or event_id
                ),
                error_code=exc.code,
                permanent=True,
                now=current_time,
            )
            session.commit()
            _notify_needs_attention_if_required(
                result=failure,
                settings=settings,
                writer=failure_writer,
            )
            failure_results.append(failure)
        except RuntimeError:
            if failure_results is None:
                raise
            session.rollback()
            stored_event_id = session.scalar(
                select(SiteServiceRequestEvent.event_id).where(
                    SiteServiceRequestEvent.id == event_id
                )
            )
            if stored_event_id is None:
                continue
            failure = _record_site_service_request_failure(
                session,
                event_id=stored_event_id,
                error_code="bitrix_unavailable",
                permanent=False,
                now=current_time,
            )
            session.commit()
            _notify_needs_attention_if_required(
                result=failure,
                settings=settings,
                writer=failure_writer,
            )
            failure_results.append(failure)
    return plans


def apply_site_service_request_worker_plans(
    session: Session,
    *,
    plans: list[SiteServiceRequestWorkerPlan],
    settings: Settings,
    reader: SiteServiceRequestBitrixReader,
    writer: SiteServiceRequestBitrixWriter,
    cipher: SiteServiceRequestCipher,
    now: datetime | None = None,
) -> list[SiteServiceRequestWorkerResult]:
    if not settings.site_service_requests_bitrix_writes_enabled:
        raise SiteServiceRequestConfigurationError(
            "site service request Bitrix writes are disabled"
        )
    field_map = resolved_site_service_request_field_map(settings)
    stage_id = str(settings.site_service_requests_bitrix_stage_map.get("new") or "").strip()
    if not stage_id:
        raise SiteServiceRequestConfigurationError(
            "site service request Bitrix NEW stage is not configured"
        )

    current_time = _as_utc(now or datetime.now(UTC))
    results: list[SiteServiceRequestWorkerResult] = []
    for plan in plans:
        try:
            result = _apply_site_service_request_worker_plan(
                session,
                plan=plan,
                settings=settings,
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
                event_id=plan.event_id,
                error_code=exc.code,
                permanent=True,
                now=current_time,
            )
            session.commit()
            _notify_needs_attention_if_required(
                result=result,
                settings=settings,
                writer=writer,
            )
        except RuntimeError:
            session.rollback()
            result = _record_site_service_request_failure(
                session,
                event_id=plan.event_id,
                error_code="bitrix_unavailable",
                permanent=False,
                now=current_time,
            )
            session.commit()
            _notify_needs_attention_if_required(
                result=result,
                settings=settings,
                writer=writer,
            )
        results.append(result)
    return results


def resolved_site_service_request_field_map(settings: Settings) -> dict[str, str]:
    field_map = {
        **_DEFAULT_FIELD_MAP,
        **{
            str(key): str(value)
            for key, value in settings.site_service_requests_bitrix_field_map.items()
            if str(value).strip()
        },
    }
    missing = sorted(key for key in _REQUIRED_WORKER_FIELD_KEYS if not field_map.get(key))
    if missing:
        raise SiteServiceRequestConfigurationError(
            "site service request Bitrix field mapping is incomplete: " + ", ".join(missing)
        )
    return field_map


def create_site_service_request_command(
    session: Session,
    *,
    case: SiteServiceRequestCase,
    reply_text: str,
    cipher: SiteServiceRequestCipher,
    now: datetime | None = None,
) -> tuple[SiteServiceRequestCommand, bool]:
    normalized_reply = reply_text.strip()
    if not normalized_reply:
        raise SiteServiceRequestPermanentError("reply_text_empty")
    reply = normalized_reply.encode("utf-8")
    reply_sha256 = hashlib.sha256(reply).hexdigest()
    command_key = f"site-support-reply:{case.source_ticket_id}:{reply_sha256}"
    existing = session.scalar(
        select(SiteServiceRequestCommand).where(
            SiteServiceRequestCommand.command_key == command_key
        )
    )
    if existing is not None:
        return existing, True

    current_time = _as_utc(now or datetime.now(UTC))
    command = SiteServiceRequestCommand(
        case_id=case.id,
        command_key=command_key,
        reply_encrypted=cipher.encrypt(reply, event_id=command_key),
        reply_sha256=reply_sha256,
        status="pending",
        created_at=current_time,
        updated_at=current_time,
    )
    session.add(command)
    session.flush()
    return command, False


def sync_staged_site_service_request_files(
    session: Session,
    *,
    settings: Settings,
    writer: SiteServiceRequestBitrixWriter,
    now: datetime | None = None,
    limit: int | None = None,
    cleanup_paths: list[Path] | None = None,
) -> list[dict[str, Any]]:
    if not settings.site_service_requests_bitrix_writes_enabled:
        return []
    folder_id = settings.site_service_requests_bitrix_root_folder_id
    if folder_id is None:
        return []
    field_map = resolved_site_service_request_field_map(settings)
    batch_limit = limit or settings.site_service_requests_worker_batch_size
    current_time = _as_utc(now or datetime.now(UTC))
    files = session.scalars(
        select(SiteServiceRequestFile)
        .join(SiteServiceRequestCase)
        .where(
            SiteServiceRequestFile.status.in_(("staged", "failed")),
            SiteServiceRequestFile.temporary_path.is_not(None),
            SiteServiceRequestCase.bitrix_item_id.is_not(None),
        )
        .order_by(SiteServiceRequestFile.created_at, SiteServiceRequestFile.id)
        .limit(batch_limit)
        .with_for_update(skip_locked=True)
    ).all()
    results: list[dict[str, Any]] = []
    for file in files:
        path = Path(str(file.temporary_path))
        try:
            content = path.read_bytes()
            if len(content) != file.byte_size or hashlib.sha256(content).hexdigest() != file.sha256:
                raise SiteServiceRequestPermanentError("file_payload_invalid")
            deterministic_name = (
                f"ticket-{file.case.source_ticket_id}-message-{file.source_message_id}-"
                f"file-{file.source_file_id}-{file.safe_filename}"
            )[:255]
            disk_file_id, disk_url = writer.upload_file(
                folder_id=folder_id,
                deterministic_name=deterministic_name,
                content=content,
            )
            file_ids = [
                row.bitrix_object_id
                for row in file.case.files
                if row.bitrix_object_id and row.id != file.id
            ]
            file_ids.append(disk_file_id)
            item = writer.update_item_fields(
                entity_type_id=settings.site_service_requests_bitrix_entity_type_id,
                item_id=int(file.case.bitrix_item_id),
                fields={field_map["files"]: sorted(set(file_ids))},
            )
            if not _item_field_contains(item, field_map["files"], disk_file_id):
                raise RuntimeError("bitrix_file_readback_failed")
            file.bitrix_file_id = disk_file_id
            file.bitrix_object_id = disk_file_id
            file.status = "uploaded"
            file.last_error_code = None
            file.updated_at = current_time
            file.temporary_path = None
            if cleanup_paths is not None:
                cleanup_paths.append(path)
            results.append(
                {
                    "fileId": file.source_file_id,
                    "status": "uploaded",
                    "bitrixFileId": disk_file_id,
                    "diskUrlAvailable": bool(disk_url),
                }
            )
        except SiteServiceRequestPermanentError as exc:
            file.status = "failed"
            file.last_error_code = exc.code
            file.updated_at = current_time
            file.case.sync_status = "file_sync_error"
            file.case.last_error_code = "file_sync_error"
            file.case.updated_at = current_time
            file.temporary_path = None
            if cleanup_paths is not None:
                cleanup_paths.append(path)
            _write_file_sync_error_to_item(
                file=file,
                settings=settings,
                writer=writer,
                field_map=field_map,
            )
            results.append(
                {
                    "fileId": file.source_file_id,
                    "status": "failed",
                    "errorCode": exc.code,
                }
            )
        except (OSError, RuntimeError):
            file.status = "failed"
            file.last_error_code = "file_sync_error"
            file.updated_at = current_time
            file.case.sync_status = "file_sync_error"
            file.case.last_error_code = "file_sync_error"
            file.case.updated_at = current_time
            _write_file_sync_error_to_item(
                file=file,
                settings=settings,
                writer=writer,
                field_map=field_map,
            )
            results.append(
                {
                    "fileId": file.source_file_id,
                    "status": "failed",
                    "errorCode": "file_sync_error",
                }
            )
    session.flush()
    return results


def cleanup_uploaded_site_service_request_files(paths: list[Path]) -> None:
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            continue


def _write_file_sync_error_to_item(
    *,
    file: SiteServiceRequestFile,
    settings: Settings,
    writer: SiteServiceRequestBitrixWriter,
    field_map: dict[str, str],
) -> None:
    if file.case.bitrix_item_id is None:
        return
    sync_status_value = settings.site_service_requests_bitrix_enum_map.get(
        "sync_status_file_sync_error",
        "file_sync_error",
    )
    try:
        writer.update_item_fields(
            entity_type_id=settings.site_service_requests_bitrix_entity_type_id,
            item_id=int(file.case.bitrix_item_id),
            fields={
                field_map["site_sync_status"]: sync_status_value,
                field_map["site_sync_error"]: "file_sync_error",
            },
        )
    except RuntimeError:
        return


def collect_site_service_request_outbound_commands(
    session: Session,
    *,
    settings: Settings,
    writer: SiteServiceRequestBitrixWriter,
    cipher: SiteServiceRequestCipher,
    now: datetime | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    if not (
        settings.site_service_requests_bitrix_writes_enabled
        and settings.site_service_requests_outbound_replies_enabled
    ):
        return []
    field_map = resolved_site_service_request_field_map(settings)
    required_fields = ("site_reply_text", "site_reply_action", "site_reply_status")
    if any(not field_map.get(key) for key in required_fields):
        raise SiteServiceRequestConfigurationError(
            "site service request outbound field mapping is incomplete"
        )
    enum_map = settings.site_service_requests_bitrix_enum_map
    send_value = str(enum_map.get("reply_action_send") or "").strip()
    pending_value = str(enum_map.get("reply_status_pending") or "").strip()
    sent_value = str(enum_map.get("reply_status_sent") or "").strip()
    error_value = str(enum_map.get("reply_status_error") or "").strip()
    if not send_value or not pending_value or not sent_value or not error_value:
        raise SiteServiceRequestConfigurationError(
            "site service request outbound enum mapping is incomplete"
        )

    cases = session.scalars(
        select(SiteServiceRequestCase)
        .where(SiteServiceRequestCase.bitrix_item_id.is_not(None))
        .order_by(SiteServiceRequestCase.updated_at, SiteServiceRequestCase.id)
        .limit(limit or settings.site_service_requests_worker_batch_size)
    ).all()
    results: list[dict[str, Any]] = []
    for case in cases:
        item = writer.get_item(
            entity_type_id=settings.site_service_requests_bitrix_entity_type_id,
            item_id=int(case.bitrix_item_id),
        )
        action = _item_field_value(item, field_map["site_reply_action"])
        if str(action or "") != send_value:
            continue
        reply_text = str(_item_field_value(item, field_map["site_reply_text"]) or "").strip()
        if not reply_text:
            continue
        command, duplicate = create_site_service_request_command(
            session,
            case=case,
            reply_text=reply_text,
            cipher=cipher,
            now=now,
        )
        reply_status = error_value if command.status == "failed" else pending_value
        writer.update_item_fields(
            entity_type_id=settings.site_service_requests_bitrix_entity_type_id,
            item_id=int(case.bitrix_item_id),
            fields={
                field_map["site_reply_action"]: None,
                field_map["site_reply_status"]: reply_status,
                field_map["site_sync_error"]: (
                    command.last_error_code if command.status == "failed" else None
                ),
            },
        )
        results.append(
            {
                "commandId": command.id,
                "ticketId": case.source_ticket_id,
                "status": command.status,
                "duplicate": duplicate,
            }
        )
    session.flush()
    return results


def reconcile_site_service_request_assignments(
    session: Session,
    *,
    settings: Settings,
    reader: SiteServiceRequestBitrixReader,
    writer: SiteServiceRequestBitrixWriter,
    now: datetime | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    if not settings.site_service_requests_bitrix_writes_enabled:
        return []
    field_map = resolved_site_service_request_field_map(settings)
    success_stage_id = str(settings.site_service_requests_bitrix_stage_map.get("success") or "")
    failure_stage_id = str(settings.site_service_requests_bitrix_stage_map.get("failure") or "")
    closed_stage_ids = {value for value in (success_stage_id, failure_stage_id) if value}
    fallback_open_stage_id = str(settings.site_service_requests_bitrix_stage_map.get("new") or "")
    current_time = _as_utc(now or datetime.now(UTC))
    statuses = reader.timeman_statuses(settings.site_service_requests_first_line_user_ids)
    cases = session.scalars(
        select(SiteServiceRequestCase)
        .where(
            SiteServiceRequestCase.bitrix_item_id.is_not(None),
            SiteServiceRequestCase.first_response_at.is_(None),
        )
        .order_by(SiteServiceRequestCase.first_seen_at, SiteServiceRequestCase.id)
        .limit(limit or settings.site_service_requests_worker_batch_size)
        .with_for_update(skip_locked=True)
    ).all()
    last_assignment = session.scalar(
        select(SiteServiceRequestCase)
        .where(SiteServiceRequestCase.assigned_user_id.is_not(None))
        .order_by(SiteServiceRequestCase.round_robin_seq.desc())
        .limit(1)
    )
    max_sequence = int(
        session.scalar(select(func.max(SiteServiceRequestCase.round_robin_seq))) or 0
    )
    virtual_last_assigned_user_id = (
        last_assignment.assigned_user_id if last_assignment is not None else None
    )
    next_round_robin_seq = max_sequence + 1
    results: list[dict[str, Any]] = []
    for case in cases:
        was_escalated = case.escalated_at is not None
        item = writer.get_item(
            entity_type_id=settings.site_service_requests_bitrix_entity_type_id,
            item_id=int(case.bitrix_item_id),
        )
        current_stage_id = str(_item_field_value(item, "stageId") or "")
        close_reverted = False
        if current_stage_id and current_stage_id not in closed_stage_ids:
            case.last_open_stage_id = current_stage_id
        decision = decide_site_service_assignment(
            case=case,
            configured_user_ids=settings.site_service_requests_first_line_user_ids,
            timeman_statuses=statuses,
            last_assigned_user_id=virtual_last_assigned_user_id,
            next_round_robin_seq=next_round_robin_seq,
            escalation_user_id=settings.site_service_requests_escalation_user_id,
            first_response_hours=settings.site_service_requests_first_response_hours,
            timezone_name=settings.site_service_requests_timezone,
            now=current_time,
        )
        if case.assigned_user_id is None and decision.assigned_user_id is not None:
            virtual_last_assigned_user_id = decision.assigned_user_id
            next_round_robin_seq += 1
        case.assigned_user_id = decision.assigned_user_id
        case.assignment_state = decision.state
        case.intake_mode = decision.intake_mode
        case.first_response_due_at = decision.first_response_due_at
        case.sla_paused_at = decision.sla_paused_at
        case.escalated_at = decision.escalated_at
        case.round_robin_seq = decision.round_robin_seq
        case.updated_at = current_time
        fields: dict[str, Any] = {
            field_map["first_response_due_at"]: decision.first_response_due_at,
        }
        if current_stage_id in closed_stage_ids:
            return_stage_id = case.last_open_stage_id or fallback_open_stage_id
            if return_stage_id:
                fields["stageId"] = return_stage_id
                close_reverted = True
        if decision.assigned_user_id is not None:
            fields["assignedById"] = decision.assigned_user_id
        writer.update_item_fields(
            entity_type_id=settings.site_service_requests_bitrix_entity_type_id,
            item_id=int(case.bitrix_item_id),
            fields=fields,
        )
        escalated_now = not was_escalated and decision.escalated_at is not None
        if escalated_now:
            writer.add_timeline_comment(
                entity_type_id=settings.site_service_requests_bitrix_entity_type_id,
                item_id=int(case.bitrix_item_id),
                comment="SLA первого ответа просрочен. Ответственность передана резерву.",
            )
            if settings.site_service_requests_escalation_user_id is not None:
                writer.notify_user(
                    user_id=settings.site_service_requests_escalation_user_id,
                    message=(
                        "Просрочен SLA первого ответа по сервисному обращению "
                        f"сайта #{case.source_ticket_id}."
                    ),
                )
        results.append(
            {
                "caseId": case.id,
                "ticketId": case.source_ticket_id,
                "assignmentState": decision.state,
                "assignedUserId": decision.assigned_user_id,
                "escalated": escalated_now,
                "closeReverted": close_reverted,
            }
        )
    session.flush()
    return results


def _apply_site_service_request_worker_plan(
    session: Session,
    *,
    plan: SiteServiceRequestWorkerPlan,
    settings: Settings,
    reader: SiteServiceRequestBitrixReader,
    writer: SiteServiceRequestBitrixWriter,
    cipher: SiteServiceRequestCipher,
    field_map: dict[str, str],
    stage_id: str,
    now: datetime,
) -> SiteServiceRequestWorkerResult:
    event = session.scalar(
        select(SiteServiceRequestEvent)
        .where(SiteServiceRequestEvent.event_id == plan.event_id)
        .with_for_update()
    )
    if event is None or event.payload_encrypted is None:
        raise SiteServiceRequestPermanentError("event_payload_unavailable")
    payload = _decrypt_site_service_request_payload(event, cipher=cipher)
    case = session.scalar(
        select(SiteServiceRequestCase)
        .where(SiteServiceRequestCase.id == event.case_id)
        .with_for_update()
    )
    if case is None:
        raise SiteServiceRequestPermanentError("case_not_found")
    confirmed_outbound_reply = _confirm_site_service_request_command_readback(
        session,
        case=case,
        payload=payload,
    )

    if case.crm_contact_id is not None:
        contact = ContactMatch(
            status="matched",
            contact_id=case.crm_contact_id,
            company_id=case.crm_company_id,
            candidate_ids=(case.crm_contact_id,),
        )
    else:
        contact = reader.find_contact(
            phone=payload.ticket.phone,
            email=payload.ticket.email,
        )
    contact_id = contact.contact_id
    company_id = contact.company_id
    contact_status = contact.status
    if contact_status == "not_found":
        contact_id = writer.create_contact(payload)
        company_id = None
        contact_status = "created"

    order = reader.find_order(
        contact_id=contact_id,
        order_number=payload.ticket.order_number,
        order_field=settings.site_service_requests_crm_order_field,
    )
    case.crm_contact_id = contact_id
    case.crm_company_id = company_id
    case.crm_deal_id = order.deal_id

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
    timeman_statuses = reader.timeman_statuses(
        settings.site_service_requests_first_line_user_ids
    )
    was_escalated = case.escalated_at is not None
    assignment = decide_site_service_assignment(
        case=case,
        configured_user_ids=settings.site_service_requests_first_line_user_ids,
        timeman_statuses=timeman_statuses,
        last_assigned_user_id=(
            last_assignment.assigned_user_id if last_assignment is not None else None
        ),
        next_round_robin_seq=max_round_robin_seq + 1,
        escalation_user_id=settings.site_service_requests_escalation_user_id,
        first_response_hours=settings.site_service_requests_first_response_hours,
        timezone_name=settings.site_service_requests_timezone,
        now=now,
    )
    case.assigned_user_id = assignment.assigned_user_id
    case.assignment_state = assignment.state
    case.intake_mode = assignment.intake_mode
    case.first_response_due_at = assignment.first_response_due_at
    case.sla_paused_at = assignment.sla_paused_at
    case.escalated_at = assignment.escalated_at
    case.round_robin_seq = assignment.round_robin_seq

    file_error_code = session.scalar(
        select(SiteServiceRequestFile.last_error_code)
        .where(
            SiteServiceRequestFile.case_id == case.id,
            SiteServiceRequestFile.status == "failed",
        )
        .order_by(SiteServiceRequestFile.updated_at.desc(), SiteServiceRequestFile.id.desc())
        .limit(1)
    )

    sync_status, error_code = _case_sync_status(
        contact_status=contact_status,
        order_status=order.status,
        has_order_number=bool(payload.ticket.order_number),
        assignment_state=assignment.state,
        file_error_code=file_error_code,
    )
    fields = _site_service_request_item_fields(
        payload=payload,
        case=case,
        field_map=field_map,
        sync_status=sync_status,
        settings=settings,
        now=now,
        error_code=error_code,
        confirmed_outbound_reply=confirmed_outbound_reply,
    )
    idempotency_key = f"site-support-ticket:{payload.ticket.id}"
    item_id = writer.sync_item(
        entity_type_id=settings.site_service_requests_bitrix_entity_type_id,
        idempotency_field=field_map["idempotency_key"],
        idempotency_key=idempotency_key,
        fields=fields,
        create_only_fields={
            "categoryId": settings.site_service_requests_bitrix_working_category_id,
            "stageId": stage_id,
        },
        preferred_item_id=case.bitrix_item_id,
    )

    case.bitrix_item_id = item_id
    case.sync_status = sync_status
    case.last_error_code = error_code
    case.version += 1
    case.updated_at = now
    event.status = "processed"
    event.attempts += 1
    event.next_retry_at = None
    event.last_error_code = None
    event.processed_at = now
    event.updated_at = now
    event.payload_encrypted = None
    escalated_now = not was_escalated and assignment.escalated_at is not None
    if escalated_now:
        writer.add_timeline_comment(
            entity_type_id=settings.site_service_requests_bitrix_entity_type_id,
            item_id=item_id,
            comment="SLA первого ответа просрочен. Ответственность передана резерву.",
        )
        if settings.site_service_requests_escalation_user_id is not None:
            writer.notify_user(
                user_id=settings.site_service_requests_escalation_user_id,
                message=(
                    "Просрочен SLA первого ответа по сервисному обращению "
                    f"сайта #{case.source_ticket_id}."
                ),
            )
    session.flush()
    return SiteServiceRequestWorkerResult(
        event_id=event.event_id,
        status="processed",
        bitrix_item_id=item_id,
        error_code=error_code,
    )


def _confirm_site_service_request_command_readback(
    session: Session,
    *,
    case: SiteServiceRequestCase,
    payload: SiteServiceRequestEventPayload,
) -> bool:
    support_messages = {
        message.message_id: _as_utc(message.created_at)
        for message in payload.history
        if message.author_kind in {"support-team", "support_team"}
    }
    if not support_messages:
        return False
    first_support_response_at = min(support_messages.values())
    if (
        case.first_response_at is None
        or first_support_response_at < _as_utc(case.first_response_at)
    ):
        case.first_response_at = first_support_response_at
    case.latest_outbound_message_id = max(
        case.latest_outbound_message_id or 0,
        max(support_messages),
    )
    commands = session.scalars(
        select(SiteServiceRequestCommand).where(
            SiteServiceRequestCommand.case_id == case.id,
            SiteServiceRequestCommand.status == "applied",
            SiteServiceRequestCommand.source_message_id.in_(support_messages),
        )
    ).all()
    confirmed_at = [
        support_messages[command.source_message_id]
        for command in commands
        if command.source_message_id in support_messages
    ]
    if not confirmed_at:
        return False
    first_response_at = min(confirmed_at)
    if case.first_response_at is None or first_response_at < _as_utc(case.first_response_at):
        case.first_response_at = first_response_at
    return True


def _record_site_service_request_failure(
    session: Session,
    *,
    event_id: str,
    error_code: str,
    permanent: bool,
    now: datetime,
) -> SiteServiceRequestWorkerResult:
    event = session.scalar(
        select(SiteServiceRequestEvent)
        .where(SiteServiceRequestEvent.event_id == event_id)
        .with_for_update()
    )
    if event is None:
        return SiteServiceRequestWorkerResult(
            event_id=event_id,
            status="missing",
            bitrix_item_id=None,
            error_code="event_not_found",
        )
    event.attempts += 1
    is_expired = now - _as_utc(event.created_at) >= timedelta(hours=24)
    needs_attention = is_expired or (permanent and event.attempts >= 5)
    event.status = "needs_attention" if needs_attention else "retry"
    event.next_retry_at = (
        None
        if needs_attention
        else next_site_service_request_retry_at(attempts=event.attempts, now=now)
    )
    event.last_error_code = error_code
    event.updated_at = now
    event.case.sync_status = event.status
    event.case.last_error_code = error_code
    event.case.updated_at = now
    session.flush()
    return SiteServiceRequestWorkerResult(
        event_id=event.event_id,
        status=event.status,
        bitrix_item_id=event.case.bitrix_item_id,
        error_code=error_code,
    )


def _notify_needs_attention_if_required(
    *,
    result: SiteServiceRequestWorkerResult,
    settings: Settings,
    writer: SiteServiceRequestBitrixWriter | None,
) -> None:
    if (
        result.status != "needs_attention"
        or writer is None
        or settings.site_service_requests_escalation_user_id is None
    ):
        return
    writer.notify_user(
        user_id=settings.site_service_requests_escalation_user_id,
        message=(
            "Интеграция сервисных обращений требует внимания: "
            f"событие {result.event_id}, код {result.error_code or 'unknown'}."
        ),
    )


def _decrypt_site_service_request_payload(
    event: SiteServiceRequestEvent,
    *,
    cipher: SiteServiceRequestCipher,
) -> SiteServiceRequestEventPayload:
    if event.payload_encrypted is None:
        raise SiteServiceRequestPermanentError("event_payload_unavailable")
    try:
        return SiteServiceRequestEventPayload.model_validate_json(
            cipher.decrypt(event.payload_encrypted, event_id=event.event_id)
        )
    except (ValidationError, SiteServiceRequestConfigurationError) as exc:
        raise SiteServiceRequestPermanentError("event_payload_invalid") from exc


def _site_service_request_item_fields(
    *,
    payload: SiteServiceRequestEventPayload,
    case: SiteServiceRequestCase,
    field_map: dict[str, str],
    sync_status: str,
    settings: Settings,
    now: datetime,
    error_code: str | None,
    confirmed_outbound_reply: bool,
) -> dict[str, Any]:
    latest_customer_text = next(
        (
            message.text
            for message in reversed(payload.history)
            if message.author_kind == "customer"
        ),
        "",
    )
    history = "\n\n".join(
        f"[{message.created_at.isoformat()}] {message.author_kind}:\n{message.text}"
        for message in payload.history
    )
    contact_text = payload.ticket.phone
    if payload.ticket.email:
        contact_text += f"\n{payload.ticket.email}"
    fields: dict[str, Any] = {
        "title": f"Тикет сайта #{payload.ticket.id} — {payload.ticket.title}"[:255],
        field_map["source"]: "site-support-ticket",
        field_map["customer_contact"]: contact_text,
        field_map["crm_contact"]: case.crm_contact_id,
        field_map["crm_company"]: case.crm_company_id,
        field_map["crm_deal"]: case.crm_deal_id,
        field_map["order_refs"]: payload.ticket.order_number,
        field_map["problem_description"]: latest_customer_text,
        field_map["request_type"]: settings.site_service_requests_bitrix_enum_map.get(
            f"request_type_{payload.ticket.request_type}",
            payload.ticket.request_type,
        ),
        field_map["backend_case_id"]: case.id,
        field_map["idempotency_key"]: f"site-support-ticket:{payload.ticket.id}",
        field_map["site_ticket_id"]: str(payload.ticket.id),
        field_map["site_ticket_url"]: (
            f"{settings.site_service_requests_site_base_url.rstrip('/')}"
            f"/personal/tickets/?ID={payload.ticket.id}"
        ),
        field_map["site_history"]: history,
        field_map["site_sync_status"]: settings.site_service_requests_bitrix_enum_map.get(
            f"sync_status_{sync_status}",
            sync_status,
        ),
        field_map["site_last_sync_at"]: now,
        field_map["first_response_due_at"]: case.first_response_due_at,
        field_map["first_response_at"]: case.first_response_at,
        field_map["site_sync_error"]: error_code,
    }
    if confirmed_outbound_reply:
        sent_value = str(
            settings.site_service_requests_bitrix_enum_map.get("reply_status_sent") or ""
        ).strip()
        if not sent_value:
            raise SiteServiceRequestConfigurationError(
                "site service request sent reply enum mapping is incomplete"
            )
        fields[field_map["site_reply_action"]] = None
        fields[field_map["site_reply_status"]] = sent_value
    if case.assigned_user_id is not None:
        fields["assignedById"] = case.assigned_user_id
    rendered_fields = {key: value for key, value in fields.items() if value is not None}
    rendered_fields[field_map["site_sync_error"]] = error_code
    if confirmed_outbound_reply:
        rendered_fields[field_map["site_reply_action"]] = None
    return rendered_fields


def _case_sync_status(
    *,
    contact_status: str,
    order_status: str,
    has_order_number: bool,
    assignment_state: str,
    file_error_code: str | None,
) -> tuple[str, str | None]:
    if file_error_code:
        return "file_sync_error", "file_sync_error"
    if contact_status == "ambiguous":
        return "client_match_required", "client_match_required"
    if order_status == "ambiguous":
        return "order_match_required", "order_match_required"
    if has_order_number and order_status == "not_found":
        return "order_not_found", "order_not_found"
    if assignment_state == "waiting":
        return "assignment_waiting", "assignment_waiting"
    return "synced", None


def safe_site_service_request_plan_dict(plan: SiteServiceRequestWorkerPlan) -> dict[str, Any]:
    return {
        "eventId": plan.event_id,
        "caseId": plan.case_id,
        "ticketId": plan.ticket_id,
        "contactStatus": plan.contact_status,
        "contactId": plan.contact_id,
        "companyId": plan.company_id,
        "orderStatus": plan.order_status,
        "dealId": plan.deal_id,
        "assignmentState": plan.assignment_state,
        "assignedUserId": plan.assigned_user_id,
        "firstResponseDueAt": (
            plan.first_response_due_at.isoformat() if plan.first_response_due_at else None
        ),
        "escalated": plan.escalated,
        "actions": list(plan.actions),
    }


def next_site_service_request_retry_at(
    *,
    attempts: int,
    now: datetime | None = None,
) -> datetime:
    current_time = _as_utc(now or datetime.now(UTC))
    index = max(0, attempts - 1)
    delay = _RETRY_DELAYS_SECONDS[index] if index < len(_RETRY_DELAYS_SECONDS) else 60 * 60
    return current_time + timedelta(seconds=delay)


def render_site_service_request_plans(plans: list[SiteServiceRequestWorkerPlan]) -> str:
    return json.dumps(
        [safe_site_service_request_plan_dict(plan) for plan in plans],
        ensure_ascii=False,
        indent=2,
    )


def _item_params(*, entity_type_id: int, fields: dict[str, Any]) -> list[tuple[str, str]]:
    return [("entityTypeId", str(entity_type_id)), *_field_params(fields)]


def _field_params(fields: dict[str, Any]) -> list[tuple[str, str]]:
    params: list[tuple[str, str]] = []
    for field, value in fields.items():
        api_field = _bitrix_item_field_name(field)
        if isinstance(value, (list, tuple, set)):
            params.extend(
                (f"fields[{api_field}][]", "" if item is None else str(item))
                for item in value
            )
            continue
        if value is None:
            rendered = ""
        elif isinstance(value, datetime):
            rendered = value.isoformat()
        elif isinstance(value, bool):
            rendered = "Y" if value else "N"
        else:
            rendered = str(value)
        params.append((f"fields[{api_field}]", rendered))
    return params


def _bitrix_item_field_name(value: str) -> str:
    normalized = str(value).strip()
    if not normalized.upper().startswith("UF_"):
        return normalized
    parts = [part for part in normalized.lower().split("_") if part]
    if not parts:
        return normalized
    return parts[0] + "".join(part[:1].upper() + part[1:] for part in parts[1:])


def _disk_file_from_payload(payload: dict[str, Any]) -> tuple[str, str | None] | None:
    result = payload.get("result")
    if isinstance(result, list):
        item = result[0] if result else None
    elif isinstance(result, dict):
        items = result.get("items")
        item = items[0] if isinstance(items, list) and items else result
    else:
        item = None
    if not isinstance(item, dict):
        return None
    file_id = item.get("ID") or item.get("id") or item.get("REAL_OBJECT_ID")
    if file_id is None:
        return None
    url = item.get("DETAIL_URL") or item.get("detailUrl") or item.get("DOWNLOAD_URL")
    return str(file_id), str(url) if url else None


def _item_field_value(item: dict[str, Any], field_name: str) -> Any:
    expected = _normalized_field_key(field_name)
    for key, value in item.items():
        if _normalized_field_key(str(key)) == expected:
            return value
    return None


def _item_field_contains(item: dict[str, Any], field_name: str, expected_id: str) -> bool:
    value = _item_field_value(item, field_name)
    values = value if isinstance(value, list) else [value]
    for candidate in values:
        if isinstance(candidate, dict):
            candidate = candidate.get("id") or candidate.get("ID") or candidate.get("value")
        if str(candidate or "") == str(expected_id):
            return True
    return False


def _normalized_field_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _deal_ids(payload: dict[str, Any]) -> list[int]:
    return [deal_id for deal_id, _title in _deal_id_titles(payload)]


def _deal_id_titles(payload: dict[str, Any]) -> list[tuple[int, str]]:
    result = payload.get("result") or []
    if isinstance(result, dict):
        result = result.get("items") or []
    rows: list[tuple[int, str]] = []
    for item in result if isinstance(result, list) else []:
        if not isinstance(item, dict):
            continue
        deal_id = _positive_int(item.get("ID") or item.get("id"))
        if deal_id is not None:
            rows.append((deal_id, str(item.get("TITLE") or item.get("title") or "")))
    return rows


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
