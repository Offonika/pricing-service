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
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.exc import IntegrityError
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
_MISSING_ITEM_FIELD = object()
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
_REQUIRED_WORKER_ENUM_KEYS = {
    "sync_status_synced",
    "sync_status_client_match_required",
    "sync_status_order_match_required",
    "sync_status_order_not_found",
    "sync_status_file_sync_error",
    "sync_status_assignment_waiting",
    "request_type_warranty",
    "request_type_refund_money",
    "request_type_replacement",
    "request_type_delivery_return",
    "request_type_consultation",
    "request_type_other",
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


def preflight_site_service_request_users(
    *,
    api: SiteServiceRequestBitrixApi,
    settings: Settings,
) -> list[dict[str, Any]]:
    configured: list[tuple[str, int]] = [
        (f"first_line_{index + 1}", user_id)
        for index, user_id in enumerate(settings.site_service_requests_first_line_user_ids)
    ]
    if settings.site_service_requests_escalation_user_id is not None:
        configured.append(("escalation", settings.site_service_requests_escalation_user_id))
    if settings.site_service_requests_finance_user_id is not None:
        configured.append(("finance", settings.site_service_requests_finance_user_id))
    if len({user_id for _role, user_id in configured}) != len(configured):
        raise SiteServiceRequestConfigurationError(
            "site service request pilot user IDs must be unique"
        )

    result: list[dict[str, Any]] = []
    for role, user_id in configured:
        expected_name = str(
            settings.site_service_requests_expected_user_names.get(str(user_id)) or ""
        ).strip()
        if not expected_name:
            raise SiteServiceRequestConfigurationError(
                f"site service request expected name is missing for {role}"
            )
        response = api.call("user.get", [("ID", str(user_id))])
        raw_users = response.get("result") or []
        users = raw_users if isinstance(raw_users, list) else [raw_users]
        users = [user for user in users if isinstance(user, dict)]
        if len(users) != 1:
            raise SiteServiceRequestConfigurationError(
                f"site service request pilot user readback failed for {role}"
            )
        user = users[0]
        returned_user_id = _positive_int(user.get("ID") or user.get("id"))
        if returned_user_id != user_id:
            raise SiteServiceRequestConfigurationError(
                f"site service request pilot user readback failed for {role}"
            )
        if str(user.get("ACTIVE") or user.get("active") or "").upper() != "Y":
            raise SiteServiceRequestConfigurationError(
                f"site service request pilot user is inactive for {role}"
            )
        first_name = str(user.get("NAME") or user.get("name") or "").strip()
        last_name = str(user.get("LAST_NAME") or user.get("lastName") or "").strip()
        actual_names = {
            _normalized_person_name(f"{first_name} {last_name}"),
            _normalized_person_name(f"{last_name} {first_name}"),
        }
        if _normalized_person_name(expected_name) not in actual_names:
            raise SiteServiceRequestConfigurationError(
                f"site service request pilot user identity mismatch for {role}"
            )
        result.append({"role": role, "userId": user_id, "active": True})
    return result


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
            try:
                response = self.api.call("timeman.status", [("USER_ID", str(user_id))])
                result = response.get("result") or {}
                status = result.get("STATUS") if isinstance(result, dict) else None
                statuses[user_id] = str(status or "ERROR").upper()
            except RuntimeError:
                statuses[user_id] = "ERROR"
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
                ("fields[ENTITY_TYPE]", f"dynamic_{entity_type_id}"),
                ("fields[ENTITY_ID]", str(item_id)),
                ("fields[COMMENT]", comment),
            ],
        )

    def timeline_comment_exists(
        self,
        *,
        entity_type_id: int,
        item_id: int,
        marker: str,
    ) -> bool:
        start: int | None = None
        visited_offsets: set[int] = set()
        page_count = 0
        while True:
            current_offset = start or 0
            if current_offset in visited_offsets:
                raise RuntimeError("bitrix_timeline_pagination_cycle")
            visited_offsets.add(current_offset)
            page_count += 1
            params = [
                ("filter[ENTITY_TYPE]", f"dynamic_{entity_type_id}"),
                ("filter[ENTITY_ID]", str(item_id)),
                ("order[ID]", "DESC"),
            ]
            if start is not None:
                params.append(("start", str(start)))
            response = self.api.call("crm.timeline.comment.list", params)
            result = response.get("result")
            nested_next = None
            if isinstance(result, dict):
                nested_next = result.get("next")
                result = result.get("items") or result.get("comments") or []
            elif not isinstance(result, list):
                result = []
            if any(
                marker in str(row.get("COMMENT") or row.get("comment") or "")
                for row in result
                if isinstance(row, dict)
            ):
                return True
            next_start = nested_next if nested_next is not None else response.get("next")
            if next_start is None:
                return False
            if page_count >= 100:
                raise RuntimeError("bitrix_timeline_pagination_limit")
            if isinstance(next_start, bool):
                raise RuntimeError("bitrix_timeline_pagination_invalid")
            try:
                parsed_next = int(next_start)
            except (TypeError, ValueError):
                raise RuntimeError("bitrix_timeline_pagination_invalid") from None
            if parsed_next < 0 or parsed_next in visited_offsets:
                raise RuntimeError("bitrix_timeline_pagination_cycle")
            start = parsed_next

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
            readback = self._readback_item(
                entity_type_id=entity_type_id,
                item_id=preferred_item_id,
            )
            if str(_item_field_value(readback, idempotency_field) or "") != idempotency_key:
                raise RuntimeError("bitrix_item_readback_failed")
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
            readback = self._readback_item(entity_type_id=entity_type_id, item_id=item_id)
            if str(_item_field_value(readback, idempotency_field) or "") != idempotency_key:
                raise RuntimeError("bitrix_item_readback_failed")
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
        readback = self._readback_item(entity_type_id=entity_type_id, item_id=item_id)
        if str(_item_field_value(readback, idempotency_field) or "") != idempotency_key:
            raise RuntimeError("bitrix_item_readback_failed")
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
    allow_reassignment: bool = False,
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
        should_assign = assigned_user_id is None or (
            allow_reassignment
            and case.first_response_at is None
            and assignment_state != "escalated"
            and assigned_user_id not in available
        )
        if should_assign:
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
        if assigned_user_id is None or (
            allow_reassignment
            and case.first_response_at is None
            and assignment_state != "escalated"
        ):
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
    first_active_event_per_case = (
        select(
            SiteServiceRequestEvent.case_id.label("case_id"),
            func.min(SiteServiceRequestEvent.source_message_id).label("source_message_id"),
        )
        .where(SiteServiceRequestEvent.status.in_(("pending", "retry")))
        .group_by(SiteServiceRequestEvent.case_id)
        .subquery()
    )
    event_ids = session.scalars(
        select(SiteServiceRequestEvent.id)
        .join(
            first_active_event_per_case,
            and_(
                first_active_event_per_case.c.case_id == SiteServiceRequestEvent.case_id,
                first_active_event_per_case.c.source_message_id
                == SiteServiceRequestEvent.source_message_id,
            ),
        )
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
            _notify_needs_attention_if_required(
                result=failure,
                settings=settings,
                writer=failure_writer,
            )
            session.commit()
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
            _notify_needs_attention_if_required(
                result=failure,
                settings=settings,
                writer=failure_writer,
            )
            session.commit()
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
    validate_site_service_request_enum_map(settings)
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
            if result.bitrix_item_id is not None:
                delivered_case_id = session.scalar(
                    select(SiteServiceRequestCase.id).where(
                        SiteServiceRequestCase.bitrix_item_id == result.bitrix_item_id
                    )
                )
                if delivered_case_id is not None:
                    try:
                        _deliver_site_service_request_escalation(
                            session,
                            case_id=delivered_case_id,
                            settings=settings,
                            writer=writer,
                            now=current_time,
                        )
                    except RuntimeError:
                        session.rollback()
                        failed_delivery_case = session.get(
                            SiteServiceRequestCase, delivered_case_id
                        )
                        if failed_delivery_case is not None:
                            failed_delivery_case.assignment_checked_at = current_time
                            failed_delivery_case.assignment_last_error_code = (
                                "escalation_delivery_failed"
                            )
                            failed_delivery_case.updated_at = current_time
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
            _notify_needs_attention_if_required(
                result=result,
                settings=settings,
                writer=writer,
            )
            session.commit()
        except RuntimeError:
            session.rollback()
            result = _record_site_service_request_failure(
                session,
                event_id=plan.event_id,
                error_code="bitrix_unavailable",
                permanent=False,
                now=current_time,
            )
            _notify_needs_attention_if_required(
                result=result,
                settings=settings,
                writer=writer,
            )
            session.commit()
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


def validate_site_service_request_enum_map(settings: Settings) -> None:
    missing = sorted(
        key
        for key in _REQUIRED_WORKER_ENUM_KEYS
        if not str(settings.site_service_requests_bitrix_enum_map.get(key) or "").strip()
    )
    if missing:
        raise SiteServiceRequestConfigurationError(
            "site service request Bitrix enum mapping is incomplete: " + ", ".join(missing)
        )


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
    try:
        with session.begin_nested():
            session.add(command)
            session.flush()
        return command, False
    except IntegrityError:
        existing = session.scalar(
            select(SiteServiceRequestCommand).where(
                SiteServiceRequestCommand.command_key == command_key
            )
        )
        if existing is None:
            raise
        return existing, True


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
            other_failed_files = int(
                session.scalar(
                    select(func.count(SiteServiceRequestFile.id)).where(
                        SiteServiceRequestFile.case_id == file.case_id,
                        SiteServiceRequestFile.id != file.id,
                        SiteServiceRequestFile.status == "failed",
                    )
                )
                or 0
            )
            recovered_from_file_error = (
                other_failed_files == 0 and file.case.sync_status == "file_sync_error"
            )
            update_fields: dict[str, Any] = {
                field_map["files"]: sorted(set(file_ids)),
            }
            if recovered_from_file_error:
                update_fields.update(
                    {
                        field_map["site_sync_status"]: _site_service_request_enum_value(
                            settings,
                            f"sync_status_{file.case.base_sync_status}",
                        ),
                        field_map["site_sync_error"]: file.case.base_error_code,
                    }
                )
            item = writer.update_item_fields(
                entity_type_id=settings.site_service_requests_bitrix_entity_type_id,
                item_id=int(file.case.bitrix_item_id),
                fields=update_fields,
            )
            if not _item_field_contains(item, field_map["files"], disk_file_id):
                raise RuntimeError("bitrix_file_readback_failed")
            file.bitrix_file_id = disk_file_id
            file.bitrix_object_id = disk_file_id
            file.status = "uploaded"
            file.last_error_code = None
            file.updated_at = current_time
            if recovered_from_file_error:
                file.case.sync_status = file.case.base_sync_status
                file.case.last_error_code = file.case.base_error_code
                file.case.updated_at = current_time
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
    sync_status_value = _site_service_request_enum_value(
        settings,
        "sync_status_file_sync_error",
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


def _site_service_request_reply_sha256(
    item: dict[str, Any],
    *,
    field_name: str,
) -> str:
    reply_text = _item_field_value(item, field_name, default=_MISSING_ITEM_FIELD)
    if reply_text is _MISSING_ITEM_FIELD:
        raise RuntimeError("bitrix_reply_text_readback_failed")
    normalized_reply = str(reply_text or "").strip().encode("utf-8")
    return hashlib.sha256(normalized_reply).hexdigest()


def _restore_site_service_request_send_action(
    *,
    case: SiteServiceRequestCase,
    settings: Settings,
    writer: SiteServiceRequestBitrixWriter,
    field_map: dict[str, str],
    send_value: str,
    pending_value: str,
) -> None:
    restored_item = writer.update_item_fields(
        entity_type_id=settings.site_service_requests_bitrix_entity_type_id,
        item_id=int(case.bitrix_item_id),
        fields={
            field_map["site_reply_action"]: send_value,
            field_map["site_reply_status"]: pending_value,
        },
    )
    if (
        str(_item_field_value(restored_item, field_map["site_reply_action"]) or "") != send_value
        or str(_item_field_value(restored_item, field_map["site_reply_status"]) or "")
        != pending_value
        or _item_field_value(
            restored_item,
            field_map["site_reply_text"],
            default=_MISSING_ITEM_FIELD,
        )
        is _MISSING_ITEM_FIELD
    ):
        raise RuntimeError("bitrix_reply_restore_readback_failed")


def _collect_site_service_request_outbound_case(
    session: Session,
    *,
    case: SiteServiceRequestCase,
    settings: Settings,
    writer: SiteServiceRequestBitrixWriter,
    cipher: SiteServiceRequestCipher,
    field_map: dict[str, str],
    send_value: str,
    pending_value: str,
    sent_value: str,
    error_value: str,
    current_time: datetime,
) -> dict[str, Any] | None:
    case_id = case.id
    item = writer.get_item(
        entity_type_id=settings.site_service_requests_bitrix_entity_type_id,
        item_id=int(case.bitrix_item_id),
    )
    action = _item_field_value(item, field_map["site_reply_action"])
    uncleared_command = session.scalar(
        select(SiteServiceRequestCommand)
        .where(
            SiteServiceRequestCommand.case_id == case.id,
            SiteServiceRequestCommand.card_action_cleared_at.is_(None),
        )
        .order_by(SiteServiceRequestCommand.created_at.desc(), SiteServiceRequestCommand.id.desc())
        .limit(1)
        .with_for_update()
    )
    latest_command = session.scalar(
        select(SiteServiceRequestCommand)
        .where(SiteServiceRequestCommand.case_id == case.id)
        .order_by(
            SiteServiceRequestCommand.created_at.desc(),
            SiteServiceRequestCommand.id.desc(),
        )
        .limit(1)
    )
    if (
        uncleared_command is None
        and latest_command is not None
        and latest_command.status == "failed"
        and str(action or "") != send_value
        and str(_item_field_value(item, field_map["site_reply_status"]) or "") != error_value
    ):
        updated_item = writer.update_item_fields(
            entity_type_id=settings.site_service_requests_bitrix_entity_type_id,
            item_id=int(case.bitrix_item_id),
            fields={
                field_map["site_reply_action"]: None,
                field_map["site_reply_status"]: error_value,
                field_map["site_sync_error"]: latest_command.last_error_code,
            },
        )
        if (
            not _item_field_is_cleared(updated_item, field_map["site_reply_action"])
            or str(_item_field_value(updated_item, field_map["site_reply_status"]) or "")
            != error_value
        ):
            raise RuntimeError("bitrix_reply_status_readback_failed")
        result = {
            "commandId": latest_command.id,
            "ticketId": case.source_ticket_id,
            "status": "failed",
            "duplicate": True,
        }
        session.commit()
        return result
    duplicate = uncleared_command is not None
    command = uncleared_command
    if command is None:
        if str(action or "") != send_value:
            session.commit()
            return None
        reply_text = str(_item_field_value(item, field_map["site_reply_text"]) or "").strip()
        if not reply_text:
            session.commit()
            return None
        command, duplicate = create_site_service_request_command(
            session,
            case=case,
            reply_text=reply_text,
            cipher=cipher,
            now=current_time,
        )
        command_id = command.id
        session.commit()
        _lock_site_service_request_outbound_sequence(session)
        case = session.scalar(
            select(SiteServiceRequestCase)
            .where(SiteServiceRequestCase.id == case_id)
            .with_for_update()
        )
        if case is None:
            session.rollback()
            return None
        command = session.scalar(
            select(SiteServiceRequestCommand)
            .where(SiteServiceRequestCommand.id == command_id)
            .with_for_update()
        )
        if command is None:
            session.rollback()
            return None

    current_item = writer.get_item(
        entity_type_id=settings.site_service_requests_bitrix_entity_type_id,
        item_id=int(case.bitrix_item_id),
    )
    current_reply_sha256 = _site_service_request_reply_sha256(
        current_item,
        field_name=field_map["site_reply_text"],
    )
    if current_reply_sha256 != command.reply_sha256:
        _restore_site_service_request_send_action(
            case=case,
            settings=settings,
            writer=writer,
            field_map=field_map,
            send_value=send_value,
            pending_value=pending_value,
        )
        result = {
            "commandId": command.id,
            "ticketId": case.source_ticket_id,
            "status": command.status,
            "duplicate": duplicate,
        }
        command.card_action_cleared_at = current_time
        session.commit()
        return result

    reply_status = {
        "applied": sent_value,
        "failed": error_value,
    }.get(command.status, pending_value)
    update_fields: dict[str, Any] = {
        field_map["site_reply_action"]: None,
        field_map["site_reply_status"]: reply_status,
    }
    if command.status == "failed":
        update_fields[field_map["site_sync_error"]] = command.last_error_code
    elif case.sync_status == "synced":
        update_fields[field_map["site_sync_error"]] = None
    updated_item = writer.update_item_fields(
        entity_type_id=settings.site_service_requests_bitrix_entity_type_id,
        item_id=int(case.bitrix_item_id),
        fields=update_fields,
    )
    updated_reply_sha256 = _site_service_request_reply_sha256(
        updated_item,
        field_name=field_map["site_reply_text"],
    )
    if updated_reply_sha256 != command.reply_sha256:
        _restore_site_service_request_send_action(
            case=case,
            settings=settings,
            writer=writer,
            field_map=field_map,
            send_value=send_value,
            pending_value=pending_value,
        )
        result = {
            "commandId": command.id,
            "ticketId": case.source_ticket_id,
            "status": command.status,
            "duplicate": duplicate,
        }
        command.card_action_cleared_at = current_time
        session.commit()
        return result
    if (
        not _item_field_is_cleared(updated_item, field_map["site_reply_action"])
        or str(_item_field_value(updated_item, field_map["site_reply_status"]) or "")
        != reply_status
    ):
        raise RuntimeError("bitrix_reply_status_readback_failed")
    result = {
        "commandId": command.id,
        "ticketId": case.source_ticket_id,
        "status": command.status,
        "duplicate": duplicate,
    }
    command.card_action_cleared_at = current_time
    session.commit()
    return result


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

    batch_limit = limit or settings.site_service_requests_worker_batch_size
    processed_case_ids: set[int] = set()
    results: list[dict[str, Any]] = []
    while len(processed_case_ids) < batch_limit:
        _lock_site_service_request_outbound_sequence(session)
        query = select(SiteServiceRequestCase).where(
            SiteServiceRequestCase.bitrix_item_id.is_not(None)
        )
        if processed_case_ids:
            query = query.where(SiteServiceRequestCase.id.not_in(processed_case_ids))
        case = session.scalar(
            query.order_by(
                SiteServiceRequestCase.outbound_checked_at.asc().nulls_first(),
                SiteServiceRequestCase.id,
            )
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if case is None:
            session.rollback()
            break
        processed_case_ids.add(case.id)
        current_time = _as_utc(now or datetime.now(UTC))
        case.outbound_checked_at = current_time
        case.outbound_last_error_code = None
        try:
            result = _collect_site_service_request_outbound_case(
                session,
                case=case,
                settings=settings,
                writer=writer,
                cipher=cipher,
                field_map=field_map,
                send_value=send_value,
                pending_value=pending_value,
                sent_value=sent_value,
                error_value=error_value,
                current_time=current_time,
            )
            if result is not None:
                results.append(result)
        except RuntimeError:
            case_id = case.id
            session.rollback()
            failed_case = session.get(SiteServiceRequestCase, case_id)
            if failed_case is not None:
                failed_case.outbound_checked_at = current_time
                failed_case.outbound_last_error_code = "outbound_reconcile_failed"
                failed_case.updated_at = current_time
                result = {
                    "caseId": failed_case.id,
                    "ticketId": failed_case.source_ticket_id,
                    "status": "retry",
                    "errorCode": "outbound_reconcile_failed",
                }
                session.commit()
                results.append(result)
    return results


def _deliver_site_service_request_escalation(
    session: Session,
    *,
    case_id: int,
    settings: Settings,
    writer: SiteServiceRequestBitrixWriter,
    now: datetime,
) -> None:
    _lock_site_service_request_assignment_sequence(session)
    case = session.scalar(
        select(SiteServiceRequestCase).where(SiteServiceRequestCase.id == case_id).with_for_update()
    )
    if case is None or case.escalated_at is None or case.bitrix_item_id is None:
        session.commit()
        return
    marker = f"[site-service-escalation:{case.id}]"
    if case.escalation_timeline_delivered_at is None:
        if not writer.timeline_comment_exists(
            entity_type_id=settings.site_service_requests_bitrix_entity_type_id,
            item_id=int(case.bitrix_item_id),
            marker=marker,
        ):
            writer.add_timeline_comment(
                entity_type_id=settings.site_service_requests_bitrix_entity_type_id,
                item_id=int(case.bitrix_item_id),
                comment=(
                    "SLA первого ответа просрочен. Ответственность передана резерву. " f"{marker}"
                ),
            )
        if not writer.timeline_comment_exists(
            entity_type_id=settings.site_service_requests_bitrix_entity_type_id,
            item_id=int(case.bitrix_item_id),
            marker=marker,
        ):
            raise RuntimeError("bitrix_escalation_timeline_readback_failed")
        case.escalation_timeline_delivered_at = now
    if (
        settings.site_service_requests_escalation_user_id is not None
        and case.escalation_notification_delivered_at is None
    ):
        writer.notify_user(
            user_id=settings.site_service_requests_escalation_user_id,
            message=(
                "Просрочен SLA первого ответа по сервисному обращению "
                f"сайта #{case.source_ticket_id}."
            ),
        )
        case.escalation_notification_delivered_at = now
    session.commit()


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
    batch_limit = limit or settings.site_service_requests_worker_batch_size
    processed_case_ids: set[int] = set()
    results: list[dict[str, Any]] = []
    pending_delivery_conditions = [
        SiteServiceRequestCase.escalation_timeline_delivered_at.is_(None)
    ]
    if settings.site_service_requests_escalation_user_id is not None:
        pending_delivery_conditions.append(
            SiteServiceRequestCase.escalation_notification_delivered_at.is_(None)
        )
    escalation_delivery_pending = and_(
        SiteServiceRequestCase.escalated_at.is_not(None),
        or_(*pending_delivery_conditions),
    )
    while len(processed_case_ids) < batch_limit:
        _lock_site_service_request_assignment_sequence(session)
        query = select(SiteServiceRequestCase).where(
            SiteServiceRequestCase.bitrix_item_id.is_not(None),
            or_(
                SiteServiceRequestCase.first_response_at.is_(None),
                escalation_delivery_pending,
            ),
        )
        if processed_case_ids:
            query = query.where(SiteServiceRequestCase.id.not_in(processed_case_ids))
        case = session.scalar(
            query.order_by(
                SiteServiceRequestCase.assignment_checked_at.asc().nulls_first(),
                SiteServiceRequestCase.id,
            )
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if case is None:
            session.rollback()
            break
        case_id = case.id
        processed_case_ids.add(case_id)
        try:
            if case.first_response_at is not None:
                delivery_ticket_id = case.source_ticket_id
                delivery_assignment_state = case.assignment_state
                delivery_assigned_user_id = case.assigned_user_id
                case.assignment_checked_at = current_time
                case.assignment_last_error_code = None
                case.updated_at = current_time
                _deliver_site_service_request_escalation(
                    session,
                    case_id=case_id,
                    settings=settings,
                    writer=writer,
                    now=current_time,
                )
                results.append(
                    {
                        "caseId": case_id,
                        "ticketId": delivery_ticket_id,
                        "assignmentState": delivery_assignment_state,
                        "assignedUserId": delivery_assigned_user_id,
                        "escalated": False,
                        "closeReverted": False,
                        "deliveryRetried": True,
                    }
                )
                continue
            last_assignment = session.scalar(
                select(SiteServiceRequestCase)
                .where(
                    SiteServiceRequestCase.assigned_user_id.is_not(None),
                    SiteServiceRequestCase.id != case.id,
                )
                .order_by(SiteServiceRequestCase.round_robin_seq.desc())
                .limit(1)
            )
            next_round_robin_seq = (
                int(session.scalar(select(func.max(SiteServiceRequestCase.round_robin_seq))) or 0)
                + 1
            )
            was_escalated = case.escalated_at is not None
            item = writer.get_item(
                entity_type_id=settings.site_service_requests_bitrix_entity_type_id,
                item_id=int(case.bitrix_item_id),
            )
            current_stage_id = str(_item_field_value(item, "stageId") or "")
            if current_stage_id and current_stage_id not in closed_stage_ids:
                case.last_open_stage_id = current_stage_id
            decision = decide_site_service_assignment(
                case=case,
                configured_user_ids=settings.site_service_requests_first_line_user_ids,
                timeman_statuses=statuses,
                last_assigned_user_id=(
                    last_assignment.assigned_user_id if last_assignment is not None else None
                ),
                next_round_robin_seq=next_round_robin_seq,
                escalation_user_id=settings.site_service_requests_escalation_user_id,
                first_response_hours=settings.site_service_requests_first_response_hours,
                timezone_name=settings.site_service_requests_timezone,
                allow_reassignment=current_stage_id == fallback_open_stage_id,
                now=current_time,
            )
            case.assigned_user_id = decision.assigned_user_id
            case.assignment_state = decision.state
            case.intake_mode = decision.intake_mode
            case.first_response_due_at = decision.first_response_due_at
            case.sla_paused_at = decision.sla_paused_at
            case.escalated_at = decision.escalated_at
            case.round_robin_seq = decision.round_robin_seq
            case.assignment_checked_at = current_time
            case.assignment_last_error_code = None
            _apply_assignment_base_status(case, assignment_state=decision.state)
            case.updated_at = current_time
            close_reverted = False
            fields: dict[str, Any] = {
                field_map["first_response_due_at"]: decision.first_response_due_at,
            }
            if case.sync_status in {
                "synced",
                "client_match_required",
                "order_match_required",
                "order_not_found",
                "file_sync_error",
                "assignment_waiting",
            }:
                fields[field_map["site_sync_status"]] = _site_service_request_enum_value(
                    settings, f"sync_status_{case.sync_status}"
                )
                fields[field_map["site_sync_error"]] = case.last_error_code
            if current_stage_id in closed_stage_ids:
                return_stage_id = case.last_open_stage_id or fallback_open_stage_id
                if return_stage_id:
                    fields["stageId"] = return_stage_id
                    close_reverted = True
            if decision.assigned_user_id is not None:
                fields["assignedById"] = decision.assigned_user_id
            escalated_now = not was_escalated and decision.escalated_at is not None
            ticket_id = case.source_ticket_id
            bitrix_item_id = int(case.bitrix_item_id)
            session.commit()

            updated_item = writer.update_item_fields(
                entity_type_id=settings.site_service_requests_bitrix_entity_type_id,
                item_id=bitrix_item_id,
                fields=fields,
            )
            if close_reverted and str(_item_field_value(updated_item, "stageId") or "") != str(
                fields["stageId"]
            ):
                raise RuntimeError("bitrix_close_gate_readback_failed")
            if (
                decision.assigned_user_id is not None
                and _positive_int(_item_field_value(updated_item, "assignedById"))
                != decision.assigned_user_id
            ):
                raise RuntimeError("bitrix_assignment_readback_failed")
            if field_map["site_sync_status"] in fields:
                expected_sync_status = str(fields[field_map["site_sync_status"]])
                if (
                    str(_item_field_value(updated_item, field_map["site_sync_status"]) or "")
                    != expected_sync_status
                ):
                    raise RuntimeError("bitrix_assignment_status_readback_failed")
            _deliver_site_service_request_escalation(
                session,
                case_id=case_id,
                settings=settings,
                writer=writer,
                now=current_time,
            )
            results.append(
                {
                    "caseId": case_id,
                    "ticketId": ticket_id,
                    "assignmentState": decision.state,
                    "assignedUserId": decision.assigned_user_id,
                    "escalated": escalated_now,
                    "closeReverted": close_reverted,
                }
            )
        except RuntimeError:
            session.rollback()
            failed_case = session.get(SiteServiceRequestCase, case_id)
            if failed_case is not None:
                failed_case.assignment_checked_at = current_time
                failed_case.assignment_last_error_code = "assignment_reconcile_failed"
                failed_case.updated_at = current_time
                session.commit()
                results.append(
                    {
                        "caseId": failed_case.id,
                        "ticketId": failed_case.source_ticket_id,
                        "assignmentState": failed_case.assignment_state,
                        "assignedUserId": failed_case.assigned_user_id,
                        "escalated": False,
                        "closeReverted": False,
                        "errorCode": "assignment_reconcile_failed",
                    }
                )
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
    if event is None:
        raise SiteServiceRequestPermanentError("event_payload_unavailable")
    if event.status == "processed" and event.payload_encrypted is None:
        return SiteServiceRequestWorkerResult(
            event_id=event.event_id,
            status="processed",
            bitrix_item_id=event.case.bitrix_item_id,
            error_code=event.case.last_error_code,
        )
    if event.payload_encrypted is None:
        raise SiteServiceRequestPermanentError("event_payload_unavailable")
    payload = _decrypt_site_service_request_payload(event, cipher=cipher)
    _lock_site_service_request_assignment_sequence(session)
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
    timeman_statuses = reader.timeman_statuses(settings.site_service_requests_first_line_user_ids)
    current_stage_id = stage_id
    if case.bitrix_item_id is not None:
        current_item = writer.get_item(
            entity_type_id=settings.site_service_requests_bitrix_entity_type_id,
            item_id=int(case.bitrix_item_id),
        )
        current_stage_id = str(_item_field_value(current_item, "stageId") or "")
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
        allow_reassignment=current_stage_id == stage_id,
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
    readback_item = writer.get_item(
        entity_type_id=settings.site_service_requests_bitrix_entity_type_id,
        item_id=item_id,
    )
    if case.first_response_at is not None and not _item_field_value(
        readback_item,
        field_map["first_response_at"],
    ):
        raise RuntimeError("bitrix_first_response_readback_failed")
    for clearable_field in (field_map["crm_deal"], field_map["order_refs"]):
        expected_value = fields.get(clearable_field)
        if expected_value is None and not _item_field_is_cleared(readback_item, clearable_field):
            raise RuntimeError("bitrix_order_link_clear_readback_failed")
    if confirmed_outbound_reply:
        sent_value = _site_service_request_enum_value(settings, "reply_status_sent")
        if (
            not _item_field_is_cleared(readback_item, field_map["site_reply_action"])
            or str(_item_field_value(readback_item, field_map["site_reply_status"]) or "")
            != sent_value
        ):
            raise RuntimeError("bitrix_reply_status_readback_failed")

    case.bitrix_item_id = item_id
    case.base_sync_status = sync_status
    case.base_error_code = error_code
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
        and message.is_visible_to_customer
        and _as_utc(message.created_at) >= _as_utc(case.first_seen_at)
    }
    if not support_messages:
        return False
    first_support_response_at = min(support_messages.values())
    if case.first_response_at is None or first_support_response_at < _as_utc(
        case.first_response_at
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
    event.case.base_sync_status = event.status
    event.case.base_error_code = error_code
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
        f"[{message.created_at.isoformat()}] {message.author_kind}"
        f" ({'видимо клиенту' if message.is_visible_to_customer else 'скрыто'}):\n"
        f"{message.text}"
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
        field_map["request_type"]: _site_service_request_enum_value(
            settings,
            f"request_type_{payload.ticket.request_type}",
        ),
        field_map["backend_case_id"]: case.id,
        field_map["idempotency_key"]: f"site-support-ticket:{payload.ticket.id}",
        field_map["site_ticket_id"]: str(payload.ticket.id),
        field_map["site_ticket_url"]: (
            f"{settings.site_service_requests_site_base_url.rstrip('/')}"
            f"/personal/tickets/?ID={payload.ticket.id}"
        ),
        field_map["site_history"]: history,
        field_map["site_sync_status"]: _site_service_request_enum_value(
            settings,
            f"sync_status_{sync_status}",
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
    rendered_fields[field_map["crm_deal"]] = case.crm_deal_id
    rendered_fields[field_map["order_refs"]] = payload.ticket.order_number
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


def _apply_assignment_base_status(
    case: SiteServiceRequestCase,
    *,
    assignment_state: str,
) -> None:
    if case.base_sync_status not in {"pending", "synced", "assignment_waiting"}:
        return
    if assignment_state == "waiting":
        case.base_sync_status = "assignment_waiting"
        case.base_error_code = "assignment_waiting"
    else:
        case.base_sync_status = "synced"
        case.base_error_code = None
    if case.sync_status != "file_sync_error":
        case.sync_status = case.base_sync_status
        case.last_error_code = case.base_error_code


def _site_service_request_enum_value(settings: Settings, key: str) -> str:
    value = str(settings.site_service_requests_bitrix_enum_map.get(key) or "").strip()
    if not value:
        raise SiteServiceRequestConfigurationError(
            f"site service request Bitrix enum mapping is missing: {key}"
        )
    return value


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
                (f"fields[{api_field}][]", "" if item is None else str(item)) for item in value
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


def _item_field_value(
    item: dict[str, Any],
    field_name: str,
    *,
    default: Any = None,
) -> Any:
    expected = _normalized_field_key(field_name)
    for key, value in item.items():
        if _normalized_field_key(str(key)) == expected:
            return value
    return default


def _item_field_is_cleared(item: dict[str, Any], field_name: str) -> bool:
    value = _item_field_value(item, field_name, default=_MISSING_ITEM_FIELD)
    return value is not _MISSING_ITEM_FIELD and value in (None, "", 0, "0", [], {})


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


def _normalized_person_name(value: str) -> str:
    return " ".join(str(value).casefold().replace("ё", "е").split())


def _lock_site_service_request_assignment_sequence(session: Session) -> None:
    bind = session.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": 3_223_001_134},
        )


def _lock_site_service_request_outbound_sequence(session: Session) -> None:
    bind = session.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": 3_223_001_135},
        )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
