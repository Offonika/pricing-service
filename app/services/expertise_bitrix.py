from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models import ExpertiseCase, ExpertiseCaseEvent
from app.services.expertise_sla import (
    DEFAULT_GEO_GROUP,
    delivery_deadline_at,
    normalize_datetime,
    resolve_geo_group,
    review_deadline_at,
)

STATUS_CREATED = "created"
STATUS_RECEIVED_BY_OKK = "received_by_okk"
STATUS_UNDER_REVIEW = "under_review"
STATUS_DECISION_READY = "decision_ready"
STATUS_CLIENT_NOTIFIED = "client_notified"
STATUS_RETURNED_TO_CENTRAL_DEFECT = "returned_to_central_defect"
STATUS_RETURNED_TO_STORE = "returned_to_store"
STATUS_MANUAL_REVIEW = "manual_review"

REVIEW_WARNING_STATUSES = {STATUS_RECEIVED_BY_OKK, STATUS_UNDER_REVIEW, STATUS_MANUAL_REVIEW}
TERMINAL_STATUSES = {STATUS_RETURNED_TO_CENTRAL_DEFECT, STATUS_RETURNED_TO_STORE}

EVENT_RECEIVED_BY_OKK = "received_by_okk"
EVENT_MOVED_TO_REVIEW = "moved_to_review"
EVENT_DECISION_RECORDED = "decision_recorded"
EVENT_CLIENT_NOTIFIED = "client_notified"
EVENT_MANUAL_REVIEW_REQUIRED = "manual_review_required"
EVENT_REVIEW_WARNING = "review_warning"
EVENT_REVIEW_ESCALATION = "review_escalation"
EVENT_REVIEW_TOP_ESCALATION = "review_top_escalation"
EVENT_CLIENT_NOTIFY_REMINDER = "client_notify_reminder"
EVENT_CLIENT_NOTIFY_ESCALATION = "client_notify_escalation"
EVENT_AUTOMATION_ERROR = "automation_error"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_datetime(value: datetime | None) -> datetime | None:
    return normalize_datetime(value)


def _format_datetime(value: datetime | None) -> str:
    normalized = _normalize_datetime(value)
    return "" if normalized is None else normalized.isoformat(sep="T")


def _format_field_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, datetime):
        return _format_datetime(value)
    return str(value)


def _rest_field_name(field_name: str) -> str:
    normalized = field_name.strip()
    if not normalized:
        return normalized
    if not normalized.upper().startswith("UF_"):
        return normalized
    parts = normalized.lower().split("_")
    head, *tail = parts
    return head + "".join(part.capitalize() for part in tail)


def _truncate_error(value: str | None, *, limit: int = 1000) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    return normalized[:limit]


def _build_event_key(case_id: int, event_type: str, idempotency_key: str | None) -> str | None:
    if not idempotency_key:
        return None
    return f"{idempotency_key}:{case_id}:{event_type}"


def _get_event_by_key(
    session: Session,
    *,
    case_id: int,
    event_type: str,
    idempotency_key: str | None,
) -> ExpertiseCaseEvent | None:
    event_key = _build_event_key(case_id, event_type, idempotency_key)
    if event_key is None:
        return None
    return session.scalar(
        select(ExpertiseCaseEvent).where(ExpertiseCaseEvent.idempotency_key == event_key)
    )


def _append_event(
    session: Session,
    *,
    case_row: ExpertiseCase,
    event_type: str,
    comment: str | None,
    meta: dict[str, Any] | None,
    idempotency_key: str | None,
    source: str = "automation",
    event_at: datetime | None = None,
) -> ExpertiseCaseEvent:
    event = ExpertiseCaseEvent(
        expertise_case_id=case_row.id,
        event_type=event_type,
        event_at=event_at or utcnow(),
        actor_external_id=None,
        source=source,
        comment=comment,
        meta=meta,
        idempotency_key=_build_event_key(case_row.id, event_type, idempotency_key),
    )
    session.add(event)
    return event


def _latest_event_at(
    session: Session,
    *,
    case_id: int,
    event_types: tuple[str, ...],
) -> datetime | None:
    stmt = (
        select(ExpertiseCaseEvent)
        .where(
            ExpertiseCaseEvent.expertise_case_id == case_id,
            ExpertiseCaseEvent.event_type.in_(event_types),
        )
        .order_by(ExpertiseCaseEvent.event_at.desc(), ExpertiseCaseEvent.id.desc())
    )
    event = session.scalar(stmt)
    return None if event is None else _normalize_datetime(event.event_at)


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


def _decision_label(case_row: ExpertiseCase) -> str | None:
    if case_row.decision_label:
        return case_row.decision_label
    if case_row.decision_code == "approved":
        return "Принято"
    if case_row.decision_code == "rejected":
        return "Отказано"
    return None


def _owner_name(case_row: ExpertiseCase) -> str | None:
    payload = case_row.payload if isinstance(case_row.payload, dict) else {}
    value = payload.get("responsible_name") or payload.get("owner_name")
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _payload_posted(case_row: ExpertiseCase) -> bool | None:
    payload = case_row.payload if isinstance(case_row.payload, dict) else {}
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


def _is_operational_case(case_row: ExpertiseCase) -> bool:
    if case_row.current_status != STATUS_CREATED:
        return True
    posted = _payload_posted(case_row)
    if posted is not None:
        return not posted
    return case_row.decision_label is None


def _case_title(case_row: ExpertiseCase) -> str:
    number = case_row.onec_expertise_number or case_row.onec_expertise_ref or case_row.external_id
    store = case_row.store_name or case_row.store_external_id or "-"
    customer = case_row.customer_name or "-"
    return f"Экспертиза {number} / {store} / {customer}"


def _is_overdue(case_row: ExpertiseCase, *, now: datetime | None = None) -> bool:
    due_at = _normalize_datetime(case_row.due_at)
    current_time = _normalize_datetime(now or utcnow())
    return bool(
        due_at is not None
        and current_time is not None
        and due_at < current_time
        and case_row.current_status not in TERMINAL_STATUSES
    )


def _notify_deadline_at(
    session: Session,
    case_row: ExpertiseCase,
    *,
    notify_warning_hours: int,
) -> datetime:
    anchor = _status_anchor_at(session, case_row)
    return anchor + timedelta(hours=notify_warning_hours)


def _review_alarm_geo_group(
    case_row: ExpertiseCase,
    *,
    config: ExpertiseBitrixConfig,
) -> tuple[str, bool]:
    return resolve_geo_group(
        store_external_id=case_row.store_external_id,
        store_group_map=config.sla_store_group_map,
    )


def _days_for_geo_group(days_map: dict[str, int], geo_group: str) -> int:
    if geo_group in days_map:
        return int(days_map[geo_group])
    return int(days_map.get(DEFAULT_GEO_GROUP, 0))


def _daily_alarm_occurrence_at(
    *,
    anchor_at: datetime,
    days_after_anchor: int,
    now: datetime,
) -> datetime | None:
    threshold_at = anchor_at + timedelta(days=days_after_anchor)
    if threshold_at > now:
        return None
    elapsed_days = int((now - threshold_at).total_seconds() // 86400)
    return threshold_at + timedelta(days=max(elapsed_days, 0))


def _delivery_due_at(
    *,
    case_row: ExpertiseCase,
    config: ExpertiseBitrixConfig,
) -> tuple[datetime, str, bool]:
    anchor_at = (
        _normalize_datetime(case_row.created_at_source)
        or _normalize_datetime(case_row.created_at)
        or utcnow().replace(tzinfo=None)
    )
    return delivery_deadline_at(
        anchor_at=anchor_at,
        store_external_id=case_row.store_external_id,
        store_group_map=config.sla_store_group_map,
        delivery_days_map=config.sla_delivery_days_map,
    )


def _review_due_at(
    session: Session,
    case_row: ExpertiseCase,
    *,
    config: ExpertiseBitrixConfig,
    anchor_at: datetime | None = None,
) -> tuple[datetime, str, bool]:
    effective_anchor = anchor_at or _status_anchor_at(session, case_row)
    return review_deadline_at(
        anchor_at=effective_anchor,
        store_external_id=case_row.store_external_id,
        store_group_map=config.sla_store_group_map,
        review_days_map=config.sla_review_days_map,
    )


@dataclass(frozen=True)
class ExpertiseBitrixConfig:
    webhook_url: str | None
    entity_type_id: int | None
    category_id: int | None
    field_map: dict[str, str]
    stage_map: dict[str, str]
    root_folder_id: int | None
    notify_responsible_user_id: int | None
    notify_auditor_user_ids: list[int]
    store_department_map: dict[str, int]
    sla_store_group_map: dict[str, str]
    sla_delivery_days_map: dict[str, int]
    sla_review_days_map: dict[str, int]
    review_warning_hours: int
    notify_warning_hours: int
    notify_escalation_hours: int
    review_primary_days_map: dict[str, int]
    review_escalation_days_map: dict[str, int]
    review_top_escalation_days_map: dict[str, int]
    review_primary_user_ids: list[int]
    review_escalation_user_ids: list[int]
    review_top_escalation_user_ids: list[int]

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url and self.entity_type_id and self.field_map and self.stage_map)

    @classmethod
    def from_settings(cls, settings: Settings) -> ExpertiseBitrixConfig:
        return cls(
            webhook_url=settings.expertise_bitrix_webhook_url,
            entity_type_id=settings.expertise_bitrix_entity_type_id,
            category_id=settings.expertise_bitrix_category_id,
            field_map=dict(settings.expertise_bitrix_field_map or {}),
            stage_map=dict(settings.expertise_bitrix_stage_map or {}),
            root_folder_id=settings.expertise_bitrix_root_folder_id,
            notify_responsible_user_id=settings.expertise_bitrix_notify_responsible_user_id,
            notify_auditor_user_ids=list(settings.expertise_bitrix_notify_auditor_user_ids or []),
            store_department_map=dict(settings.expertise_bitrix_store_department_map or {}),
            sla_store_group_map=dict(settings.expertise_sla_store_group_map or {}),
            sla_delivery_days_map=dict(settings.expertise_sla_delivery_days_map or {}),
            sla_review_days_map=dict(settings.expertise_sla_review_days_map or {}),
            review_warning_hours=settings.expertise_alarm_review_warning_hours,
            notify_warning_hours=settings.expertise_alarm_notify_warning_hours,
            notify_escalation_hours=settings.expertise_alarm_notify_escalation_hours,
            review_primary_days_map=dict(settings.expertise_alarm_review_primary_days_map or {}),
            review_escalation_days_map=dict(
                settings.expertise_alarm_review_escalation_days_map or {}
            ),
            review_top_escalation_days_map=dict(
                settings.expertise_alarm_review_top_escalation_days_map or {}
            ),
            review_primary_user_ids=list(settings.expertise_alarm_review_primary_user_ids or []),
            review_escalation_user_ids=list(
                settings.expertise_alarm_review_escalation_user_ids or []
            ),
            review_top_escalation_user_ids=list(
                settings.expertise_alarm_review_top_escalation_user_ids or []
            ),
        )


class BitrixRestClient:
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url.rstrip("/")

    def call(self, method: str, params: list[tuple[str, str]] | None = None) -> dict[str, Any]:
        url = f"{self.webhook_url}/{method}.json"
        data = None
        if params:
            data = urllib.parse.urlencode(params, doseq=True).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST" if data is not None else "GET",
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if payload.get("error"):
            raise RuntimeError(
                f"Bitrix24 {method}: {payload['error']} {payload.get('error_description', '')}"
            )
        return payload

    def list_items_by_ref(
        self,
        *,
        entity_type_id: int,
        ref_field: str,
        ref_value: str,
    ) -> list[dict[str, Any]]:
        resolved_ref_field = _rest_field_name(ref_field)
        response = self.call(
            "crm.item.list",
            [
                ("entityTypeId", str(entity_type_id)),
                (f"filter[{resolved_ref_field}]", ref_value),
                ("select[]", "id"),
                ("select[]", "detailUrl"),
            ],
        )
        result = response.get("result") or {}
        items = result.get("items")
        return items if isinstance(items, list) else []

    def add_smart_process_item(
        self,
        *,
        entity_type_id: int,
        fields: dict[str, Any],
    ) -> tuple[str, str | None]:
        params = [("entityTypeId", str(entity_type_id))]
        for key, value in fields.items():
            params.append((f"fields[{_rest_field_name(key)}]", _format_field_value(value)))
        response = self.call("crm.item.add", params)
        item = response.get("result", {}).get("item", {}) or {}
        item_id = str(item.get("id") or response.get("result"))
        if not item_id:
            raise RuntimeError("Bitrix24 crm.item.add returned empty item id")
        return item_id, item.get("detailUrl")

    def update_smart_process_item(
        self,
        *,
        entity_type_id: int,
        item_id: str,
        fields: dict[str, Any],
    ) -> None:
        params = [("entityTypeId", str(entity_type_id)), ("id", str(item_id))]
        for key, value in fields.items():
            params.append((f"fields[{_rest_field_name(key)}]", _format_field_value(value)))
        self.call("crm.item.update", params)

    def add_subfolder(self, *, parent_folder_id: int, name: str) -> tuple[str, str | None]:
        response = self.call(
            "disk.folder.addsubfolder",
            [("id", str(parent_folder_id)), ("data[NAME]", name)],
        )
        result = response.get("result") or {}
        folder_id = str(
            result.get("ID")
            or result.get("id")
            or result.get("ID_STR")
            or result.get("storage", {}).get("ID")
            or ""
        )
        if not folder_id:
            raise RuntimeError("Bitrix24 disk.folder.addsubfolder returned empty folder id")
        folder_url = (
            result.get("DETAIL_URL")
            or result.get("detailUrl")
            or result.get("DETAIL_URL_SHORT")
            or None
        )
        return folder_id, folder_url

    def get_folder(self, *, folder_id: int | str) -> tuple[str, str | None]:
        response = self.call("disk.folder.get", [("id", str(folder_id))])
        result = response.get("result") or {}
        current_folder_id = str(
            result.get("ID") or result.get("id") or result.get("REAL_OBJECT_ID") or folder_id
        )
        folder_url = (
            result.get("DETAIL_URL")
            or result.get("detailUrl")
            or result.get("DETAIL_URL_SHORT")
            or None
        )
        return current_folder_id, folder_url

    def find_subfolder_by_name(
        self,
        *,
        parent_folder_id: int,
        name: str,
    ) -> tuple[str, str | None] | None:
        response = self.call(
            "disk.folder.getchildren",
            [("id", str(parent_folder_id)), ("filter[NAME]", name)],
        )
        result = response.get("result") or []
        if not isinstance(result, list) or not result:
            return None
        folder = result[0] or {}
        folder_id = str(folder.get("ID") or folder.get("id") or folder.get("REAL_OBJECT_ID") or "")
        if not folder_id:
            return None
        folder_url = folder.get("DETAIL_URL") or folder.get("detailUrl") or None
        return folder_id, folder_url

    def list_department_user_ids(self, *, department_id: int) -> list[int]:
        response = self.call(
            "user.get",
            [
                ("FILTER[ACTIVE]", "Y"),
                ("FILTER[UF_DEPARTMENT]", str(department_id)),
                ("SELECT[]", "ID"),
                ("SELECT[]", "UF_DEPARTMENT"),
            ],
        )
        result = response.get("result") or []
        ids: list[int] = []
        for item in result:
            try:
                value = int(item.get("ID"))
            except (TypeError, ValueError):
                continue
            if value not in ids:
                ids.append(value)
        return ids

    def get_department_head_user_id(self, *, department_id: int) -> int | None:
        response = self.call(
            "department.get",
            [
                ("FILTER[ID]", str(department_id)),
            ],
        )
        result = response.get("result") or []
        if isinstance(result, dict):
            result = [result]
        if not isinstance(result, list):
            return None
        for item in result:
            if not isinstance(item, dict):
                continue
            for key in ("UF_HEAD", "HEAD", "MANAGER", "UF_DEPARTMENT_HEAD"):
                value = item.get(key)
                try:
                    return int(value)
                except (TypeError, ValueError):
                    continue
        return None

    def add_task(
        self,
        *,
        title: str,
        description: str,
        responsible_id: int,
        deadline: datetime | None,
        accomplice_ids: list[int],
        auditor_ids: list[int],
    ) -> str:
        params: list[tuple[str, str]] = [
            ("fields[TITLE]", title),
            ("fields[DESCRIPTION]", description),
            ("fields[RESPONSIBLE_ID]", str(responsible_id)),
        ]
        deadline_value = _format_datetime(deadline)
        if deadline_value:
            params.append(("fields[DEADLINE]", deadline_value))
        for user_id in accomplice_ids:
            params.append(("fields[ACCOMPLICES][]", str(user_id)))
        for user_id in auditor_ids:
            params.append(("fields[AUDITORS][]", str(user_id)))
        response = self.call("tasks.task.add", params)
        result = response.get("result")
        if isinstance(result, dict):
            result = result.get("task", {}).get("id") or result.get("id")
        task_id = str(result or "")
        if not task_id:
            raise RuntimeError("Bitrix24 tasks.task.add returned empty task id")
        return task_id

    def update_task(
        self,
        *,
        task_id: str,
        title: str,
        description: str,
        responsible_id: int,
        deadline: datetime | None,
        accomplice_ids: list[int],
        auditor_ids: list[int],
    ) -> None:
        params: list[tuple[str, str]] = [("taskId", str(task_id))]
        params.extend(
            [
                ("fields[TITLE]", title),
                ("fields[DESCRIPTION]", description),
                ("fields[RESPONSIBLE_ID]", str(responsible_id)),
            ]
        )
        deadline_value = _format_datetime(deadline)
        if deadline_value:
            params.append(("fields[DEADLINE]", deadline_value))
        for user_id in accomplice_ids:
            params.append(("fields[ACCOMPLICES][]", str(user_id)))
        for user_id in auditor_ids:
            params.append(("fields[AUDITORS][]", str(user_id)))
        self.call("tasks.task.update", params)

    def complete_task(self, *, task_id: str) -> None:
        self.call("tasks.task.complete", [("taskId", str(task_id))])

    def add_task_comment(self, *, task_id: str, message: str) -> None:
        self.call(
            "task.commentitem.add",
            [("taskId", str(task_id)), ("arFields[POST_MESSAGE]", message)],
        )

    def add_personal_notification(self, *, user_id: int, message: str) -> str:
        response = self.call(
            "im.notify.personal.add",
            [("USER_ID", str(user_id)), ("MESSAGE", message)],
        )
        return str(response.get("result") or "")


def _get_client(config: ExpertiseBitrixConfig) -> BitrixRestClient:
    if not config.webhook_url:
        raise RuntimeError("expertise bitrix webhook url is not configured")
    return BitrixRestClient(config.webhook_url)


def _smart_process_fields(
    *,
    session: Session,
    case_row: ExpertiseCase,
    config: ExpertiseBitrixConfig,
    now: datetime,
) -> dict[str, Any]:
    fields: dict[str, Any] = {}

    def put(alias: str, value: Any) -> None:
        field_name = config.field_map.get(alias)
        if field_name:
            fields[field_name] = value

    put("title", _case_title(case_row))
    put("expertise_ref", case_row.onec_expertise_ref)
    put("expertise_number", case_row.onec_expertise_number)
    put("case_id", case_row.id)
    put("sale_ref", case_row.linked_sale_ref)
    put("sale_number", case_row.linked_sale_number)
    put("order_ref", case_row.linked_customer_order_ref)
    put("order_number", case_row.linked_customer_order_number)
    put("organization_ref", case_row.organization_ref)
    put("contract_ref", case_row.contract_ref)
    put("store", case_row.store_name)
    put("customer", case_row.customer_name)
    put("phone", case_row.customer_phone)
    put("problem", case_row.problem_summary)
    put("decision_code", case_row.decision_code)
    put("decision_label", _decision_label(case_row))
    put("decision_comment", case_row.decision_comment)
    put("status", case_row.current_status)
    put("owner_ext", case_row.owner_user_external_id)
    put("owner_name", _owner_name(case_row))
    put("due_at", _normalize_datetime(case_row.due_at))
    put("overdue", _is_overdue(case_row, now=now))
    put("client_notified", case_row.client_notified)
    put("sync_at", _normalize_datetime(now))
    put("source", "pricing-service")
    put("folder_url", case_row.bitrix_disk_folder_url)

    stage_id = config.stage_map.get(case_row.current_status)
    if stage_id:
        fields["stageId"] = stage_id
    elif config.enabled:
        raise RuntimeError(f"stage mapping is missing for status={case_row.current_status}")

    if config.category_id is not None:
        fields["categoryId"] = config.category_id
    assigned_by_field = config.field_map.get("assigned_by")
    if assigned_by_field and config.notify_responsible_user_id is not None:
        fields[assigned_by_field] = config.notify_responsible_user_id
    return fields


def _task_title(case_row: ExpertiseCase) -> str:
    number = case_row.onec_expertise_number or case_row.external_id
    return f"Уведомить клиента по экспертизе {number}"


def _task_description(
    *,
    session: Session,
    case_row: ExpertiseCase,
    config: ExpertiseBitrixConfig,
) -> str:
    deadline_at = _notify_deadline_at(
        session,
        case_row,
        notify_warning_hours=config.notify_warning_hours,
    )
    lines = [
        f"Экспертиза: {case_row.onec_expertise_number or case_row.external_id}",
        f"Клиент: {case_row.customer_name or '-'}",
        f"Телефон: {case_row.customer_phone or '-'}",
        f"Решение: {_decision_label(case_row) or case_row.decision_code or '-'}",
        f"Комментарий решения: {case_row.decision_comment or '-'}",
        f"Подразделение: {case_row.store_name or case_row.store_external_id or '-'}",
        f"Срок уведомления: {_format_datetime(deadline_at)}",
    ]
    if case_row.bitrix_entity_id:
        lines.append(f"Smart-process item ID: {case_row.bitrix_entity_id}")
    if case_row.bitrix_disk_folder_url:
        lines.append(f"Папка Bitrix Disk: {case_row.bitrix_disk_folder_url}")
    return "\n".join(lines)


def _dedupe_user_ids(values: list[int | None]) -> list[int]:
    result: list[int] = []
    for value in values:
        if value is None:
            continue
        if value not in result:
            result.append(value)
    return result


def _alarm_caption(event_type: str) -> str:
    if event_type == EVENT_REVIEW_WARNING:
        return "Напоминание ОКК: требуется решение по экспертизе"
    if event_type == EVENT_REVIEW_ESCALATION:
        return "Эскалация ОКК: решение по экспертизе просрочено"
    if event_type == EVENT_REVIEW_TOP_ESCALATION:
        return "Эскалация руководству: решение по экспертизе критически просрочено"
    if event_type == EVENT_CLIENT_NOTIFY_REMINDER:
        return "Напоминание: клиент еще не оповещен"
    if event_type == EVENT_CLIENT_NOTIFY_ESCALATION:
        return "Эскалация: клиент не оповещен"
    return "Будильник по экспертизе"


def _alarm_deadline_label(event_type: str) -> str:
    if event_type in {EVENT_REVIEW_WARNING, EVENT_REVIEW_ESCALATION, EVENT_REVIEW_TOP_ESCALATION}:
        return "Контрольный срок ОКК"
    if event_type == EVENT_CLIENT_NOTIFY_REMINDER:
        return "Срок уведомления клиента"
    if event_type == EVENT_CLIENT_NOTIFY_ESCALATION:
        return "Срок эскалации"
    return "Контрольный срок"


def _alarm_notification_message(
    *,
    session: Session,
    case_row: ExpertiseCase,
    config: ExpertiseBitrixConfig,
    event_type: str,
    deadline_at: datetime,
) -> str:
    lines = [
        _alarm_caption(event_type),
        f"Экспертиза: {case_row.onec_expertise_number or case_row.external_id}",
        f"Статус: {case_row.current_status}",
        f"Подразделение: {case_row.store_name or case_row.store_external_id or '-'}",
        f"Клиент: {case_row.customer_name or '-'}",
        f"Телефон: {case_row.customer_phone or '-'}",
        f"Решение: {_decision_label(case_row) or case_row.decision_code or '-'}",
        f"{_alarm_deadline_label(event_type)}: {_format_datetime(deadline_at)}",
    ]
    if case_row.bitrix_notify_task_id:
        lines.append(f"Задача уведомления: {case_row.bitrix_notify_task_id}")
    if case_row.bitrix_entity_id:
        lines.append(f"Smart-process item ID: {case_row.bitrix_entity_id}")
    if case_row.bitrix_disk_folder_url:
        lines.append(f"Папка Bitrix Disk: {case_row.bitrix_disk_folder_url}")
    if event_type in {EVENT_CLIENT_NOTIFY_REMINDER, EVENT_CLIENT_NOTIFY_ESCALATION}:
        lines.append(
            f"Текущий дедлайн уведомления: {_format_datetime(_notify_deadline_at(session, case_row, notify_warning_hours=config.notify_warning_hours))}"
        )
    return "\n".join(lines)


def _alarm_personal_recipient_ids(
    config: ExpertiseBitrixConfig,
    *,
    event_type: str,
) -> list[int]:
    if event_type == EVENT_REVIEW_WARNING:
        return _dedupe_user_ids(
            [
                *(config.review_primary_user_ids or []),
                *([] if config.review_primary_user_ids else [config.notify_responsible_user_id]),
            ]
        )
    if event_type == EVENT_REVIEW_ESCALATION:
        return _dedupe_user_ids(config.review_escalation_user_ids or [])
    if event_type == EVENT_REVIEW_TOP_ESCALATION:
        return _dedupe_user_ids(config.review_top_escalation_user_ids or [])
    return _dedupe_user_ids(
        [config.notify_responsible_user_id, *list(config.notify_auditor_user_ids or [])]
    )


def _deliver_alarm_notification(
    *,
    session: Session,
    case_row: ExpertiseCase,
    config: ExpertiseBitrixConfig,
    client: BitrixRestClient,
    event_type: str,
    deadline_at: datetime,
) -> list[str]:
    message = _alarm_notification_message(
        session=session,
        case_row=case_row,
        config=config,
        event_type=event_type,
        deadline_at=deadline_at,
    )
    errors: list[str] = []

    for user_id in _alarm_personal_recipient_ids(config, event_type=event_type):
        try:
            client.add_personal_notification(user_id=user_id, message=message)
        except Exception as error:
            errors.append(f"personal notification failed for user_id={user_id}: {error}")

    if (
        event_type in {EVENT_CLIENT_NOTIFY_REMINDER, EVENT_CLIENT_NOTIFY_ESCALATION}
        and case_row.bitrix_notify_task_id
    ):
        try:
            client.add_task_comment(task_id=case_row.bitrix_notify_task_id, message=message)
        except Exception as error:
            errors.append(
                f"task comment failed for task_id={case_row.bitrix_notify_task_id}: {error}"
            )

    return errors


def _resolve_task_participants(
    *,
    session: Session,
    case_row: ExpertiseCase,
    config: ExpertiseBitrixConfig,
    client: BitrixRestClient,
) -> tuple[int | None, list[int], str | None]:
    store_ref = case_row.store_external_id or ""
    department_id = config.store_department_map.get(store_ref)
    if department_id is None:
        return None, [], f"bitrix department is not configured for store_ref={store_ref or '-'}"
    head_user_id = client.get_department_head_user_id(department_id=department_id)
    user_ids = client.list_department_user_ids(department_id=department_id)
    if not user_ids:
        return head_user_id, [], f"bitrix department={department_id} has no active users"
    accomplice_ids = [user_id for user_id in user_ids if user_id != head_user_id]
    if head_user_id is not None:
        return head_user_id, accomplice_ids, None
    return None, accomplice_ids, f"bitrix department={department_id} has no assigned head"


def _ensure_error_event(
    session: Session,
    *,
    case_row: ExpertiseCase,
    error_key: str,
    comment: str,
    meta: dict[str, Any] | None = None,
) -> None:
    if _get_event_by_key(
        session,
        case_id=case_row.id,
        event_type=EVENT_AUTOMATION_ERROR,
        idempotency_key=error_key,
    ):
        return
    _append_event(
        session,
        case_row=case_row,
        event_type=EVENT_AUTOMATION_ERROR,
        comment=comment,
        meta=meta,
        idempotency_key=error_key,
    )


def sync_case_to_bitrix(
    session: Session,
    *,
    case_id: int,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    resolved_settings = settings or get_settings()
    config = ExpertiseBitrixConfig.from_settings(resolved_settings)
    if not config.enabled:
        return {"status": "disabled", "case_id": case_id}

    case_row = session.scalar(select(ExpertiseCase).where(ExpertiseCase.id == case_id))
    if case_row is None:
        raise RuntimeError(f"expertise case not found: id={case_id}")
    if not _is_operational_case(case_row):
        return {"status": "skipped_inactive", "case_id": case_id}

    current_time = _normalize_datetime(now or utcnow()) or utcnow().replace(tzinfo=None)
    client = _get_client(config)

    if case_row.bitrix_disk_folder_id:
        try:
            folder_id, folder_url = client.get_folder(folder_id=case_row.bitrix_disk_folder_id)
        except Exception:
            folder_id, folder_url = case_row.bitrix_disk_folder_id, None
        case_row.bitrix_disk_folder_id = folder_id
        if folder_url:
            case_row.bitrix_disk_folder_url = folder_url
    else:
        if config.root_folder_id is None:
            raise RuntimeError("expertise bitrix root folder id is not configured")
        folder_name = (
            case_row.onec_expertise_number or case_row.external_id or f"case-{case_row.id}"
        )
        full_folder_name = f"Экспертиза {folder_name}"
        existing_folder = client.find_subfolder_by_name(
            parent_folder_id=config.root_folder_id,
            name=full_folder_name,
        )
        if existing_folder is not None:
            folder_id, folder_url = existing_folder
        else:
            folder_id, folder_url = client.add_subfolder(
                parent_folder_id=config.root_folder_id,
                name=full_folder_name,
            )
        case_row.bitrix_disk_folder_id = folder_id
        if folder_url:
            case_row.bitrix_disk_folder_url = folder_url

    fields = _smart_process_fields(
        session=session,
        case_row=case_row,
        config=config,
        now=current_time,
    )

    if (
        not case_row.bitrix_entity_id
        and case_row.onec_expertise_ref
        and config.field_map.get("expertise_ref")
    ):
        matches = client.list_items_by_ref(
            entity_type_id=config.entity_type_id or 0,
            ref_field=config.field_map["expertise_ref"],
            ref_value=case_row.onec_expertise_ref,
        )
        if matches:
            match = matches[0]
            matched_id = match.get("id")
            if matched_id:
                case_row.bitrix_entity_id = str(matched_id)

    if case_row.bitrix_entity_id:
        client.update_smart_process_item(
            entity_type_id=config.entity_type_id or 0,
            item_id=case_row.bitrix_entity_id,
            fields=fields,
        )
    else:
        item_id, _ = client.add_smart_process_item(
            entity_type_id=config.entity_type_id or 0,
            fields=fields,
        )
        case_row.bitrix_entity_id = item_id

    if case_row.current_status == STATUS_DECISION_READY:
        department_head_user_id, accomplice_ids, resolution_error = _resolve_task_participants(
            session=session,
            case_row=case_row,
            config=config,
            client=client,
        )
        responsible_id = department_head_user_id or config.notify_responsible_user_id
        if responsible_id is None:
            raise RuntimeError("expertise notify responsible user id is not configured")
        if resolution_error:
            _ensure_error_event(
                session,
                case_row=case_row,
                error_key="notify-participants",
                comment=resolution_error,
                meta={"case_id": case_row.id},
            )
        deadline = _notify_deadline_at(
            session,
            case_row,
            notify_warning_hours=config.notify_warning_hours,
        )
        if case_row.bitrix_notify_task_id:
            client.update_task(
                task_id=case_row.bitrix_notify_task_id,
                title=_task_title(case_row),
                description=_task_description(session=session, case_row=case_row, config=config),
                responsible_id=responsible_id,
                deadline=deadline,
                accomplice_ids=accomplice_ids,
                auditor_ids=config.notify_auditor_user_ids,
            )
        else:
            case_row.bitrix_notify_task_id = client.add_task(
                title=_task_title(case_row),
                description=_task_description(session=session, case_row=case_row, config=config),
                responsible_id=responsible_id,
                deadline=deadline,
                accomplice_ids=accomplice_ids,
                auditor_ids=config.notify_auditor_user_ids,
            )
    elif case_row.current_status in {
        STATUS_CLIENT_NOTIFIED,
        STATUS_RETURNED_TO_CENTRAL_DEFECT,
        STATUS_RETURNED_TO_STORE,
    }:
        if case_row.bitrix_notify_task_id:
            client.complete_task(task_id=case_row.bitrix_notify_task_id)

    case_row.bitrix_last_sync_at = current_time
    case_row.bitrix_last_error = None
    session.flush()
    return {
        "status": "synced",
        "case_id": case_row.id,
        "bitrix_entity_id": case_row.bitrix_entity_id,
        "bitrix_notify_task_id": case_row.bitrix_notify_task_id,
        "bitrix_disk_folder_id": case_row.bitrix_disk_folder_id,
    }


def _apply_alarm(
    session: Session,
    *,
    case_row: ExpertiseCase,
    event_type: str,
    idempotency_key: str,
    comment: str,
    meta: dict[str, Any],
) -> bool:
    existing = _get_event_by_key(
        session,
        case_id=case_row.id,
        event_type=event_type,
        idempotency_key=idempotency_key,
    )
    if existing is not None:
        return False
    _append_event(
        session,
        case_row=case_row,
        event_type=event_type,
        comment=comment,
        meta=meta,
        idempotency_key=idempotency_key,
    )
    return True


def scan_alarm_cases(
    session: Session,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    resolved_settings = settings or get_settings()
    config = ExpertiseBitrixConfig.from_settings(resolved_settings)
    current_time = _normalize_datetime(now or utcnow()) or utcnow().replace(tzinfo=None)
    client = _get_client(config) if config.enabled else None

    rows = session.scalars(
        select(ExpertiseCase).order_by(ExpertiseCase.due_at.asc(), ExpertiseCase.id.asc())
    ).all()
    summary = {
        "auto_received_by_okk": 0,
        "review_warning": 0,
        "review_escalation": 0,
        "review_top_escalation": 0,
        "client_notify_reminder": 0,
        "client_notify_escalation": 0,
        "synced": 0,
        "errors": 0,
    }

    for row in rows:
        if not _is_operational_case(row):
            continue
        if row.current_status in TERMINAL_STATUSES:
            continue

        changed = False
        alarm_notifications: list[tuple[str, datetime]] = []

        def apply_due_at(case_row: ExpertiseCase, expected_due_at: datetime | None) -> None:
            nonlocal changed
            if _normalize_datetime(case_row.due_at) != _normalize_datetime(expected_due_at):
                case_row.due_at = expected_due_at
                changed = True

        if row.current_status == STATUS_CREATED:
            delivery_due_at, geo_group, used_fallback = _delivery_due_at(
                case_row=row, config=config
            )
            apply_due_at(row, delivery_due_at)
            if used_fallback:
                _ensure_error_event(
                    session,
                    case_row=row,
                    error_key="sla-group-fallback",
                    comment=(
                        "SLA geo group is not configured for "
                        f"store_ref={row.store_external_id or '-'}; applied group=other"
                    ),
                    meta={
                        "case_id": row.id,
                        "store_external_id": row.store_external_id,
                        "applied_geo_group": geo_group,
                    },
                )
            if delivery_due_at <= current_time:
                previous_status = row.current_status
                row.current_status = STATUS_RECEIVED_BY_OKK
                review_due_at, review_geo_group, review_used_fallback = _review_due_at(
                    session,
                    row,
                    config=config,
                    anchor_at=delivery_due_at,
                )
                row.due_at = review_due_at
                _append_event(
                    session,
                    case_row=row,
                    event_type=EVENT_RECEIVED_BY_OKK,
                    comment="Автопереход в ОКК по нормативному сроку доставки",
                    meta={
                        "from_status": previous_status,
                        "to_status": row.current_status,
                        "geo_group": review_geo_group,
                        "delivery_deadline_at": _format_datetime(delivery_due_at),
                    },
                    idempotency_key=f"delivery-sla:{_format_datetime(delivery_due_at)}",
                    event_at=delivery_due_at,
                )
                if review_used_fallback:
                    _ensure_error_event(
                        session,
                        case_row=row,
                        error_key="sla-group-fallback",
                        comment=(
                            "SLA geo group is not configured for "
                            f"store_ref={row.store_external_id or '-'}; applied group=other"
                        ),
                        meta={
                            "case_id": row.id,
                            "store_external_id": row.store_external_id,
                            "applied_geo_group": review_geo_group,
                        },
                    )
                changed = True
                summary["auto_received_by_okk"] += 1
        elif row.current_status in REVIEW_WARNING_STATUSES:
            review_due_at, geo_group, used_fallback = _review_due_at(session, row, config=config)
            apply_due_at(row, review_due_at)
            if used_fallback:
                _ensure_error_event(
                    session,
                    case_row=row,
                    error_key="sla-group-fallback",
                    comment=(
                        "SLA geo group is not configured for "
                        f"store_ref={row.store_external_id or '-'}; applied group=other"
                    ),
                    meta={
                        "case_id": row.id,
                        "store_external_id": row.store_external_id,
                        "applied_geo_group": geo_group,
                    },
                )
        elif row.current_status == STATUS_DECISION_READY:
            apply_due_at(
                row,
                _notify_deadline_at(
                    session,
                    row,
                    notify_warning_hours=config.notify_warning_hours,
                ),
            )
        elif row.current_status == STATUS_CLIENT_NOTIFIED:
            apply_due_at(row, None)

        anchor_at = _status_anchor_at(session, row)
        if row.current_status in REVIEW_WARNING_STATUSES:
            geo_group, group_used_fallback = _review_alarm_geo_group(row, config=config)
            if group_used_fallback:
                _ensure_error_event(
                    session,
                    case_row=row,
                    error_key="sla-group-fallback",
                    comment=(
                        "SLA geo group is not configured for "
                        f"store_ref={row.store_external_id or '-'}; applied group=other"
                    ),
                    meta={
                        "case_id": row.id,
                        "store_external_id": row.store_external_id,
                        "applied_geo_group": geo_group,
                    },
                )

            review_alarm_specs = [
                (
                    EVENT_REVIEW_WARNING,
                    "review_warning",
                    "review-primary",
                    config.review_primary_days_map,
                    "ОКК не зафиксировал решение по экспертизе",
                ),
                (
                    EVENT_REVIEW_ESCALATION,
                    "review_escalation",
                    "review-escalation",
                    config.review_escalation_days_map,
                    "ОКК не зафиксировал решение, требуется контроль руководителей",
                ),
                (
                    EVENT_REVIEW_TOP_ESCALATION,
                    "review_top_escalation",
                    "review-top-escalation",
                    config.review_top_escalation_days_map,
                    "ОКК не зафиксировал решение, требуется контроль генерального директора",
                ),
            ]
            for (
                event_type,
                summary_key,
                idempotency_prefix,
                days_map,
                comment,
            ) in review_alarm_specs:
                days_after_anchor = _days_for_geo_group(days_map, geo_group)
                occurrence_at = _daily_alarm_occurrence_at(
                    anchor_at=anchor_at,
                    days_after_anchor=days_after_anchor,
                    now=current_time,
                )
                if occurrence_at is None:
                    continue
                alarm_added = _apply_alarm(
                    session,
                    case_row=row,
                    event_type=event_type,
                    idempotency_key=(
                        f"{idempotency_prefix}:{geo_group}:"
                        f"{_format_datetime(anchor_at)}:{_format_datetime(occurrence_at)}"
                    ),
                    comment=comment,
                    meta={
                        "anchor_at": _format_datetime(anchor_at),
                        "deadline_at": _format_datetime(occurrence_at),
                        "geo_group": geo_group,
                        "days_after_anchor": days_after_anchor,
                    },
                )
                changed = alarm_added or changed
                if alarm_added:
                    summary[summary_key] += 1
                    alarm_notifications.append((event_type, occurrence_at))

        if row.current_status == STATUS_DECISION_READY and not row.client_notified:
            notify_warning_at = anchor_at + timedelta(hours=config.notify_warning_hours)
            if notify_warning_at <= current_time:
                reminder_added = _apply_alarm(
                    session,
                    case_row=row,
                    event_type=EVENT_CLIENT_NOTIFY_REMINDER,
                    idempotency_key="client-notify-reminder",
                    comment="Клиент еще не оповещен после принятия решения",
                    meta={
                        "anchor_at": _format_datetime(anchor_at),
                        "deadline_at": _format_datetime(notify_warning_at),
                    },
                )
                changed = reminder_added or changed
                if reminder_added:
                    summary["client_notify_reminder"] += 1
                    alarm_notifications.append((EVENT_CLIENT_NOTIFY_REMINDER, notify_warning_at))

            notify_escalation_at = anchor_at + timedelta(hours=config.notify_escalation_hours)
            if notify_escalation_at <= current_time:
                escalation_added = _apply_alarm(
                    session,
                    case_row=row,
                    event_type=EVENT_CLIENT_NOTIFY_ESCALATION,
                    idempotency_key="client-notify-escalation",
                    comment="Клиент не оповещен, требуется эскалация",
                    meta={
                        "anchor_at": _format_datetime(anchor_at),
                        "deadline_at": _format_datetime(notify_escalation_at),
                    },
                )
                changed = escalation_added or changed
                if escalation_added:
                    summary["client_notify_escalation"] += 1
                    alarm_notifications.append(
                        (EVENT_CLIENT_NOTIFY_ESCALATION, notify_escalation_at)
                    )

        if config.enabled and (changed or _is_overdue(row, now=current_time)):
            try:
                sync_case_to_bitrix(
                    session,
                    case_id=row.id,
                    settings=resolved_settings,
                    now=current_time,
                )
                summary["synced"] += 1
            except Exception as error:
                row.bitrix_last_error = _truncate_error(str(error))
                summary["errors"] += 1

        if client is not None and alarm_notifications:
            for event_type, deadline_at in alarm_notifications:
                notify_errors = _deliver_alarm_notification(
                    session=session,
                    case_row=row,
                    config=config,
                    client=client,
                    event_type=event_type,
                    deadline_at=deadline_at,
                )
                if notify_errors:
                    row.bitrix_last_error = _truncate_error("; ".join(notify_errors))
                    _ensure_error_event(
                        session,
                        case_row=row,
                        error_key=f"{event_type}-notify",
                        comment=row.bitrix_last_error or "bitrix notification delivery failed",
                        meta={
                            "case_id": row.id,
                            "event_type": event_type,
                        },
                    )
                    summary["errors"] += 1

    session.commit()
    return summary


def sync_pending_cases(
    session: Session,
    *,
    only_failed: bool = False,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    resolved_settings = settings or get_settings()
    config = ExpertiseBitrixConfig.from_settings(resolved_settings)
    if not config.enabled:
        return {"scanned": 0, "synced": 0, "errors": 0, "disabled": 1}

    stmt = select(ExpertiseCase).order_by(ExpertiseCase.updated_at.asc(), ExpertiseCase.id.asc())
    if only_failed:
        stmt = stmt.where(
            (ExpertiseCase.bitrix_last_error.is_not(None))
            | (ExpertiseCase.bitrix_last_sync_at.is_(None))
        )
    rows = session.scalars(stmt).all()
    rows = [row for row in rows if _is_operational_case(row)]

    summary = {"scanned": len(rows), "synced": 0, "errors": 0, "disabled": 0}
    current_time = _normalize_datetime(now or utcnow()) or utcnow().replace(tzinfo=None)
    for row in rows:
        try:
            sync_case_to_bitrix(
                session,
                case_id=row.id,
                settings=resolved_settings,
                now=current_time,
            )
            summary["synced"] += 1
            session.commit()
        except Exception as error:
            row.bitrix_last_error = _truncate_error(str(error))
            summary["errors"] += 1
            session.commit()
    return summary
