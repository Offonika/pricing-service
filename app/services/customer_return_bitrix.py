from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session, selectinload

from app.core.config import Settings
from app.models.customer_return import CustomerReturnAction, CustomerReturnShipment
from app.services.customer_returns import (
    ACTION_ARRIVAL_TASK,
    ACTION_COMPLETE_RETURN_TASK,
    ACTION_ONEC_RETURN_CONTROL,
    ACTION_STORAGE_REMINDER_1D,
    ACTION_STORAGE_REMINDER_3D,
)
from app.services.expertise_bitrix import BitrixRestClient

ACTION_PENDING = "pending"
ACTION_LEASED = "leased"
ACTION_COMPLETED = "completed"
ACTION_FAILED = "failed"

_CARRIER_LABELS = {
    "russian_post": "Почта России",
    "cdek": "СДЭК",
    "yandex_delivery": "Яндекс Доставка",
}
_REMINDER_DAYS = {
    ACTION_STORAGE_REMINDER_3D: 3,
    ACTION_STORAGE_REMINDER_1D: 1,
}


class CustomerReturnBitrixError(RuntimeError):
    pass


class CustomerReturnActionLeaseLost(CustomerReturnBitrixError):
    pass


class CustomerReturnActionDependencyPending(CustomerReturnBitrixError):
    pass


class CustomerReturnBitrixApi(Protocol):
    def ensure_task(
        self,
        *,
        title: str,
        description: str,
        marker: str,
        config: CustomerReturnBitrixConfig,
        deadline: datetime | None,
    ) -> str: ...

    def ensure_comment(self, *, task_id: str, marker: str, message: str) -> str: ...

    def ensure_completed(self, *, task_id: str) -> None: ...


@dataclass(frozen=True)
class CustomerReturnBitrixConfig:
    writes_enabled: bool
    webhook_url: str | None
    group_id: int | None
    created_by_user_id: int | None
    responsible_user_id: int | None
    accomplice_user_ids: tuple[int, ...]
    auditor_user_ids: tuple[int, ...]
    batch_size: int
    lease_seconds: int
    max_attempts: int

    @classmethod
    def from_settings(cls, settings: Settings) -> CustomerReturnBitrixConfig:
        return cls(
            writes_enabled=settings.customer_return_bitrix_writes_enabled,
            webhook_url=settings.customer_return_bitrix_webhook_url,
            group_id=settings.customer_return_bitrix_group_id,
            created_by_user_id=settings.customer_return_bitrix_created_by_user_id,
            responsible_user_id=settings.customer_return_bitrix_responsible_user_id,
            accomplice_user_ids=tuple(settings.customer_return_bitrix_accomplice_user_ids),
            auditor_user_ids=tuple(settings.customer_return_bitrix_auditor_user_ids),
            batch_size=settings.customer_return_worker_batch_size,
            lease_seconds=settings.customer_return_worker_lease_seconds,
            max_attempts=settings.customer_return_worker_max_attempts,
        )

    def check(self, *, apply: bool) -> dict[str, Any]:
        errors: list[str] = []
        if apply:
            if not self.writes_enabled:
                errors.append("bitrix_writes_disabled")
            webhook_url = (self.webhook_url or "").strip()
            if not webhook_url:
                errors.append("bitrix_webhook_missing")
            elif not _valid_webhook_url(webhook_url):
                errors.append("bitrix_webhook_invalid")
            if self.group_id is None:
                errors.append("group_missing")
            if self.responsible_user_id is None:
                errors.append("responsible_user_missing")
        return {
            "ready": not errors,
            "errors": errors,
            "writesEnabled": self.writes_enabled,
            "groupConfigured": self.group_id is not None,
            "responsibleConfigured": self.responsible_user_id is not None,
            "accompliceCount": len(self.accomplice_user_ids),
            "auditorCount": len(self.auditor_user_ids),
        }


@dataclass(frozen=True)
class ClaimedCustomerReturnAction:
    action_id: int
    lease_token: str


@dataclass(frozen=True)
class CustomerReturnBitrixPlan:
    action_id: int
    shipment_id: int
    action_type: str
    operation: str
    carrier: str
    tracking_number: str
    task_id: str | None
    title: str | None
    description: str | None
    message: str | None
    marker: str
    deadline: datetime | None


class CustomerReturnBitrixWriter:
    def __init__(self, client: BitrixRestClient):
        self.client = client

    def ensure_task(
        self,
        *,
        title: str,
        description: str,
        marker: str,
        config: CustomerReturnBitrixConfig,
        deadline: datetime | None,
    ) -> str:
        existing_id = self._find_task_id(
            title=title,
            marker=marker,
            responsible_user_id=config.responsible_user_id,
            group_id=config.group_id,
        )
        if existing_id is not None:
            return existing_id
        if config.responsible_user_id is None:
            raise CustomerReturnBitrixError("responsible_user_missing")
        return self.client.add_task(
            title=title,
            description=description,
            created_by_id=config.created_by_user_id,
            responsible_id=config.responsible_user_id,
            deadline=deadline,
            accomplice_ids=list(config.accomplice_user_ids),
            auditor_ids=list(config.auditor_user_ids),
            group_id=config.group_id,
        )

    def ensure_comment(self, *, task_id: str, marker: str, message: str) -> str:
        existing_id = self._find_comment_id(task_id=task_id, marker=marker)
        if existing_id is not None:
            return existing_id
        response = self.client.call(
            "task.commentitem.add",
            [
                ("taskId", str(task_id)),
                ("arFields[POST_MESSAGE]", f"{message}\n\nСлужебная метка: {marker}"),
            ],
        )
        result = response.get("result")
        if isinstance(result, dict):
            result = result.get("ID") or result.get("id")
        comment_id = str(result or "")
        if not comment_id:
            raise CustomerReturnBitrixError("bitrix_comment_id_missing")
        return comment_id

    def ensure_completed(self, *, task_id: str) -> None:
        task = self.client.get_task(task_id=task_id)
        status = str(task.get("status") or task.get("STATUS") or "").strip().casefold()
        if status in {"5", "completed", "завершена"}:
            return
        self.client.complete_task(task_id=task_id)

    def _find_task_id(
        self,
        *,
        title: str,
        marker: str,
        responsible_user_id: int | None,
        group_id: int | None,
    ) -> str | None:
        task_filter: dict[str, Any] = {"TITLE": title}
        if responsible_user_id is not None:
            task_filter["RESPONSIBLE_ID"] = responsible_user_id
        if group_id is not None:
            task_filter["GROUP_ID"] = group_id
        response = self.client.call_json(
            "tasks.task.list",
            {
                "filter": task_filter,
                "select": ["ID", "TITLE", "DESCRIPTION"],
            },
        )
        result = response.get("result")
        if isinstance(result, dict):
            rows = result.get("tasks") or result.get("items") or []
        else:
            rows = result or []
        if not isinstance(rows, list):
            raise CustomerReturnBitrixError("bitrix_task_list_invalid")
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_title = str(row.get("title") or row.get("TITLE") or "")
            description = str(row.get("description") or row.get("DESCRIPTION") or "")
            if row_title != title and marker not in description:
                continue
            task_id = str(row.get("id") or row.get("ID") or "")
            if task_id:
                return task_id
        return None

    def _find_comment_id(self, *, task_id: str, marker: str) -> str | None:
        start = 0
        seen_starts: set[int] = set()
        for _page_number in range(10):
            if start in seen_starts:
                raise CustomerReturnBitrixError("bitrix_comment_pagination_loop")
            seen_starts.add(start)
            response = self.client.call(
                "task.commentitem.getlist",
                [
                    ("TASKID", str(task_id)),
                    ("ORDER[ID]", "desc"),
                    ("start", str(start)),
                ],
            )
            rows = response.get("result")
            if not isinstance(rows, list):
                raise CustomerReturnBitrixError("bitrix_comment_list_invalid")
            for row in rows:
                if not isinstance(row, dict):
                    continue
                message = str(
                    row.get("POST_MESSAGE")
                    or row.get("postMessage")
                    or row.get("MESSAGE")
                    or row.get("message")
                    or ""
                )
                if marker in message:
                    return str(row.get("ID") or row.get("id") or "readback")
            next_start = response.get("next")
            if next_start in (None, ""):
                return None
            try:
                next_value = int(next_start)
            except (TypeError, ValueError) as exc:
                raise CustomerReturnBitrixError("bitrix_comment_pagination_invalid") from exc
            if next_value <= start:
                raise CustomerReturnBitrixError("bitrix_comment_pagination_invalid")
            start = next_value
        raise CustomerReturnBitrixError("bitrix_comment_pagination_limit")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def preview_due_actions(
    db: Session,
    *,
    now: datetime,
    limit: int,
) -> list[CustomerReturnAction]:
    current_time = _as_utc(now)
    statement = (
        select(CustomerReturnAction)
        .options(
            selectinload(CustomerReturnAction.shipment).selectinload(CustomerReturnShipment.actions)
        )
        .where(
            CustomerReturnAction.status == ACTION_PENDING,
            CustomerReturnAction.due_at <= current_time,
            or_(
                CustomerReturnAction.next_attempt_at.is_(None),
                CustomerReturnAction.next_attempt_at <= current_time,
            ),
        )
        .order_by(CustomerReturnAction.due_at, CustomerReturnAction.id)
        .limit(limit)
    )
    return list(db.scalars(statement).all())


def claim_due_actions(
    db: Session,
    *,
    now: datetime,
    limit: int,
    lease_seconds: int,
) -> list[ClaimedCustomerReturnAction]:
    current_time = _as_utc(now)
    db.execute(
        update(CustomerReturnAction)
        .where(
            CustomerReturnAction.status == ACTION_LEASED,
            CustomerReturnAction.leased_until.is_not(None),
            CustomerReturnAction.leased_until <= current_time,
        )
        .values(
            status=ACTION_PENDING,
            lease_token=None,
            leased_until=None,
            updated_at=current_time,
        )
    )
    statement = (
        select(CustomerReturnAction)
        .where(
            CustomerReturnAction.status == ACTION_PENDING,
            CustomerReturnAction.due_at <= current_time,
            or_(
                CustomerReturnAction.next_attempt_at.is_(None),
                CustomerReturnAction.next_attempt_at <= current_time,
            ),
        )
        .order_by(CustomerReturnAction.due_at, CustomerReturnAction.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    actions = list(db.scalars(statement).all())
    claimed: list[ClaimedCustomerReturnAction] = []
    for action in actions:
        lease_token = uuid4().hex
        action.status = ACTION_LEASED
        action.lease_token = lease_token
        action.leased_until = current_time + timedelta(seconds=lease_seconds)
        action.attempt_count += 1
        action.updated_at = current_time
        claimed.append(ClaimedCustomerReturnAction(action_id=action.id, lease_token=lease_token))
    db.commit()
    return claimed


def load_claimed_action(
    db: Session,
    claim: ClaimedCustomerReturnAction,
) -> CustomerReturnAction:
    action = db.scalar(
        select(CustomerReturnAction)
        .options(
            selectinload(CustomerReturnAction.shipment).selectinload(CustomerReturnShipment.actions)
        )
        .where(CustomerReturnAction.id == claim.action_id)
    )
    if action is None or action.status != ACTION_LEASED or action.lease_token != claim.lease_token:
        raise CustomerReturnActionLeaseLost("customer_return_action_lease_lost")
    return action


def complete_claimed_action(
    db: Session,
    claim: ClaimedCustomerReturnAction,
    *,
    external_reference: str,
    now: datetime,
) -> CustomerReturnAction:
    action = load_claimed_action(db, claim)
    current_time = _as_utc(now)
    action.status = ACTION_COMPLETED
    action.external_reference = external_reference
    action.completed_at = current_time
    action.next_attempt_at = None
    action.lease_token = None
    action.leased_until = None
    action.last_error = None
    action.updated_at = current_time
    db.commit()
    db.refresh(action)
    return action


def fail_claimed_action(
    db: Session,
    claim: ClaimedCustomerReturnAction,
    *,
    error_code: str,
    now: datetime,
    max_attempts: int,
) -> CustomerReturnAction:
    action = load_claimed_action(db, claim)
    current_time = _as_utc(now)
    exhausted = action.attempt_count >= max_attempts
    action.status = ACTION_FAILED if exhausted else ACTION_PENDING
    action.next_attempt_at = (
        None
        if exhausted
        else current_time + timedelta(seconds=_retry_delay_seconds(action.attempt_count))
    )
    action.lease_token = None
    action.leased_until = None
    action.last_error = error_code[:500]
    action.updated_at = current_time
    db.commit()
    db.refresh(action)
    return action


def build_delivery_plan(
    action: CustomerReturnAction,
) -> CustomerReturnBitrixPlan:
    shipment = action.shipment
    carrier_label = _CARRIER_LABELS.get(shipment.carrier, shipment.carrier)
    task_marker = f"[#mm-customer-return:{shipment.id}]"
    action_marker = f"[#mm-customer-return-action:{action.id}]"
    task_id = _arrival_task_id(shipment)

    if action.action_type == ACTION_ARRIVAL_TASK:
        title = (
            f"[Возврат][Доставка] Забрать {carrier_label}: "
            f"{shipment.tracking_number} (реестр #{shipment.id})"
        )
        description_lines = [
            "Клиентский возврат прибыл и ожидает получения.",
            "",
            f"Перевозчик: {carrier_label}",
            f"Трек-номер: {shipment.tracking_number}",
            f"ID реестра возвратов: {shipment.id}",
        ]
        if shipment.storage_deadline_at is not None:
            description_lines.append(
                f"Забрать до: {_as_utc(shipment.storage_deadline_at).isoformat()}"
            )
        if shipment.bitrix_case_id:
            description_lines.append(f"Связанная карточка Bitrix24: {shipment.bitrix_case_id}")
        if shipment.onec_order_ref:
            description_lines.append(f"Заказ 1С: {shipment.onec_order_ref}")
        description_lines.extend(
            [
                "",
                "Что сделать:",
                "1. Проверить отправление по трек-номеру.",
                "2. Забрать возврат до окончания хранения.",
                "3. В интерфейсе возврата нажать «Забрали».",
                "4. После оформления возврата проверить подтверждение в 1С.",
                "",
                f"Служебная метка: {task_marker}",
            ]
        )
        return CustomerReturnBitrixPlan(
            action_id=action.id,
            shipment_id=shipment.id,
            action_type=action.action_type,
            operation="ensure_task",
            carrier=shipment.carrier,
            tracking_number=shipment.tracking_number,
            task_id=None,
            title=title,
            description="\n".join(description_lines),
            message=None,
            marker=task_marker,
            deadline=shipment.storage_deadline_at,
        )

    if task_id is None:
        raise CustomerReturnActionDependencyPending("arrival_task_not_delivered")

    if action.action_type in _REMINDER_DAYS:
        days = _REMINDER_DAYS[action.action_type]
        message = (
            f"Напоминание: до окончания хранения клиентского возврата осталось "
            f"{days} дн. Перевозчик: {carrier_label}; трек: {shipment.tracking_number}."
        )
        operation = "ensure_comment"
    elif action.action_type == ACTION_ONEC_RETURN_CONTROL:
        message = (
            "Возврат отмечен как полученный. Проверьте оформление товарного возврата "
            f"в 1С. Трек: {shipment.tracking_number}; "
            f"заказ 1С: {shipment.onec_order_ref or 'не указан'}."
        )
        operation = "ensure_comment"
    elif action.action_type == ACTION_COMPLETE_RETURN_TASK:
        message = (
            "Read-only сверка подтвердила документ возврата в 1С: "
            f"{shipment.onec_return_ref or 'ссылка не указана'}. Задача завершается."
        )
        operation = "comment_and_complete"
    else:
        raise CustomerReturnBitrixError(f"unsupported_customer_return_action:{action.action_type}")

    return CustomerReturnBitrixPlan(
        action_id=action.id,
        shipment_id=shipment.id,
        action_type=action.action_type,
        operation=operation,
        carrier=shipment.carrier,
        tracking_number=shipment.tracking_number,
        task_id=task_id,
        title=None,
        description=None,
        message=message,
        marker=action_marker,
        deadline=None,
    )


def deliver_plan(
    api: CustomerReturnBitrixApi,
    plan: CustomerReturnBitrixPlan,
    *,
    config: CustomerReturnBitrixConfig,
) -> str:
    if plan.operation == "ensure_task":
        assert plan.title is not None
        assert plan.description is not None
        return api.ensure_task(
            title=plan.title,
            description=plan.description,
            marker=plan.marker,
            config=config,
            deadline=plan.deadline,
        )
    if plan.task_id is None or plan.message is None:
        raise CustomerReturnBitrixError("customer_return_plan_incomplete")
    comment_id = api.ensure_comment(
        task_id=plan.task_id,
        marker=plan.marker,
        message=plan.message,
    )
    if plan.operation == "comment_and_complete":
        api.ensure_completed(task_id=plan.task_id)
    return f"{plan.task_id}:comment:{comment_id}"


def safe_plan_dict(plan: CustomerReturnBitrixPlan) -> dict[str, Any]:
    return {
        "actionId": plan.action_id,
        "shipmentId": plan.shipment_id,
        "actionType": plan.action_type,
        "operation": plan.operation,
        "carrier": plan.carrier,
        "trackingNumber": plan.tracking_number,
        "taskId": plan.task_id,
        "deadline": plan.deadline.isoformat() if plan.deadline else None,
    }


def _arrival_task_id(shipment: CustomerReturnShipment) -> str | None:
    for action in shipment.actions:
        if action.action_type != ACTION_ARRIVAL_TASK:
            continue
        if action.status != ACTION_COMPLETED or not action.external_reference:
            continue
        return str(action.external_reference).split(":", 1)[0]
    return None


def _retry_delay_seconds(attempt_count: int) -> int:
    delays = (60, 300, 900, 3600, 10800)
    return delays[min(max(attempt_count - 1, 0), len(delays) - 1)]


def _valid_webhook_url(value: str) -> bool:
    parsed = urlsplit(value)
    return (
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and "/rest/" in f"{parsed.path.rstrip('/')}/"
        and parsed.query == ""
        and parsed.fragment == ""
    )
