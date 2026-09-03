from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.order_assembly_queue import OrderAssemblyCrmOutbox

READY_STATUSES = {"05", "06"}
DELIVERY_CODES = {
    "PICKUP",
    "CDEK_PVZ",
    "CDEK_COURIER",
    "RUSSIAN_POST",
    "MM_COURIER",
    "DOSTAVISTA",
    "YANDEX_DELIVERY_PVZ",
    "YANDEX_DELIVERY_COURIER",
    "YANDEX_TAXI",
    "MARSHRUTKA_PTG",
    "OTHER",
}
PAYMENT_MODES = {"included", "carrier_direct", "by_agreement", "free"}
RETRYABLE_STATUSES = ("pending", "retry")


class AssemblyOutboxError(ValueError):
    pass


class AssemblyOutboxConflict(AssemblyOutboxError):
    pass


@dataclass(frozen=True, slots=True)
class AssemblyOutboxInput:
    event_key: str
    event_at: datetime
    assembly_source: str
    assembly_ref: str
    site_order_number: str
    execution_status: str
    delivery_code: str
    payment_mode: str | None = None
    onec_order_number: str | None = None
    crm_status: str = "assembled"


@dataclass(frozen=True, slots=True)
class AssemblyOutboxEnqueueResult:
    row: OrderAssemblyCrmOutbox
    created: bool


def enqueue_assembly_event(
    session: Session,
    payload: AssemblyOutboxInput,
    *,
    now: datetime | None = None,
) -> AssemblyOutboxEnqueueResult:
    normalized = _normalize(payload)
    existing = session.scalar(
        select(OrderAssemblyCrmOutbox).where(
            OrderAssemblyCrmOutbox.event_key == normalized.event_key
        )
    )
    if existing is not None:
        if _identity(existing) != _input_identity(normalized):
            raise AssemblyOutboxConflict("event_key already exists with another payload")
        return AssemblyOutboxEnqueueResult(row=existing, created=False)

    created_at = _ensure_aware(now or datetime.now(timezone.utc))
    row = OrderAssemblyCrmOutbox(
        event_key=normalized.event_key,
        crm_status=normalized.crm_status,
        event_at=normalized.event_at,
        assembly_source=normalized.assembly_source,
        assembly_ref=normalized.assembly_ref,
        onec_order_number=normalized.onec_order_number,
        site_order_number=normalized.site_order_number,
        execution_status=normalized.execution_status,
        delivery_code=normalized.delivery_code,
        payment_mode=normalized.payment_mode,
        status="pending",
        attempt_count=0,
        next_attempt_at=created_at,
        updated_at=created_at,
    )
    session.add(row)
    session.flush()
    return AssemblyOutboxEnqueueResult(row=row, created=True)


def due_events(
    session: Session,
    *,
    now: datetime | None = None,
    limit: int = 100,
) -> list[OrderAssemblyCrmOutbox]:
    due_at = _ensure_aware(now or datetime.now(timezone.utc))
    return list(
        session.scalars(
            select(OrderAssemblyCrmOutbox)
            .where(
                OrderAssemblyCrmOutbox.status.in_(RETRYABLE_STATUSES),
                or_(
                    OrderAssemblyCrmOutbox.next_attempt_at.is_(None),
                    OrderAssemblyCrmOutbox.next_attempt_at <= due_at,
                ),
            )
            .order_by(OrderAssemblyCrmOutbox.next_attempt_at, OrderAssemblyCrmOutbox.id)
            .limit(max(1, min(int(limit), 500)))
            .with_for_update(skip_locked=True)
        ).all()
    )


def apply_delivery_result(
    row: OrderAssemblyCrmOutbox,
    response: dict[str, Any],
    *,
    now: datetime | None = None,
    max_attempts: int = 8,
    retry_base_seconds: int = 60,
) -> None:
    attempted_at = _ensure_aware(now or datetime.now(timezone.utc))
    row.attempt_count += 1
    row.last_attempt_at = attempted_at
    row.updated_at = attempted_at
    row.crm_response = json.dumps(response, ensure_ascii=False, sort_keys=True)[:8000]
    if bool(response.get("ok")):
        row.status = "delivered"
        row.delivered_at = attempted_at
        row.next_attempt_at = attempted_at
        row.last_error = None
        return

    row.last_error = _response_error(response)[:2000]
    if row.attempt_count >= max(1, int(max_attempts)):
        row.status = "manual_review"
        row.next_attempt_at = attempted_at
        return

    row.status = "retry"
    delay = max(1, int(retry_base_seconds)) * (2 ** max(0, row.attempt_count - 1))
    row.next_attempt_at = attempted_at + timedelta(seconds=min(delay, 24 * 60 * 60))


def deliver_due_events(
    session: Session,
    *,
    sender: Callable[[OrderAssemblyCrmOutbox], dict[str, Any]],
    now: datetime | None = None,
    limit: int = 100,
    max_attempts: int = 8,
    retry_base_seconds: int = 60,
) -> dict[str, int]:
    rows = due_events(session, now=now, limit=limit)
    result = {"selected": len(rows), "delivered": 0, "retry": 0, "manual_review": 0}
    for row in rows:
        try:
            response = sender(row)
        except Exception as exc:  # noqa: BLE001 - external CRM boundary.
            response = {"ok": False, "error": type(exc).__name__}
        apply_delivery_result(
            row,
            response,
            now=now,
            max_attempts=max_attempts,
            retry_base_seconds=retry_base_seconds,
        )
        result[row.status] += 1
    session.flush()
    return result


def crm_payload(row: OrderAssemblyCrmOutbox) -> dict[str, str]:
    payload = {
        "order": row.site_order_number,
        "status": row.crm_status,
        "assembly_source": row.assembly_source,
        "assembly_ref": row.assembly_ref,
        "idempotency_key": row.event_key,
        "assembled_at": _format_datetime(row.event_at),
        "execution_status": row.execution_status,
        "delivery_code": row.delivery_code,
    }
    if row.payment_mode:
        payload["payment_mode"] = row.payment_mode
    if row.onec_order_number:
        payload["onec_order_number"] = row.onec_order_number
    return payload


def _normalize(payload: AssemblyOutboxInput) -> AssemblyOutboxInput:
    normalized = AssemblyOutboxInput(
        event_key=_required(payload.event_key, "event_key", 160),
        event_at=_ensure_aware(payload.event_at),
        assembly_source=_required(payload.assembly_source, "assembly_source", 32),
        assembly_ref=_required(payload.assembly_ref, "assembly_ref", 64),
        site_order_number=_required(payload.site_order_number, "site_order_number", 64),
        execution_status=_required(payload.execution_status, "execution_status", 2),
        delivery_code=_required(payload.delivery_code, "delivery_code", 32).upper(),
        payment_mode=_optional_lower(payload.payment_mode, 32),
        onec_order_number=_optional_text(payload.onec_order_number, 64),
        crm_status=_required(payload.crm_status, "crm_status", 32).lower(),
    )
    if normalized.crm_status != "assembled":
        raise AssemblyOutboxError("only assembled events are accepted")
    if normalized.execution_status not in READY_STATUSES:
        raise AssemblyOutboxError("execution_status must be 05 or 06")
    if normalized.delivery_code not in DELIVERY_CODES:
        raise AssemblyOutboxError("unknown delivery_code")
    if normalized.payment_mode and normalized.payment_mode not in PAYMENT_MODES:
        raise AssemblyOutboxError("unknown payment_mode")
    if normalized.delivery_code == "MM_COURIER" and normalized.execution_status != "06":
        raise AssemblyOutboxError("MM_COURIER requires execution_status=06")
    return normalized


def _identity(row: OrderAssemblyCrmOutbox) -> tuple[Any, ...]:
    return (
        _ensure_aware(row.event_at),
        row.assembly_source,
        row.assembly_ref,
        row.site_order_number,
        row.execution_status,
        row.delivery_code,
        row.payment_mode,
        row.onec_order_number,
        row.crm_status,
    )


def _input_identity(payload: AssemblyOutboxInput) -> tuple[Any, ...]:
    return (
        payload.event_at,
        payload.assembly_source,
        payload.assembly_ref,
        payload.site_order_number,
        payload.execution_status,
        payload.delivery_code,
        payload.payment_mode,
        payload.onec_order_number,
        payload.crm_status,
    )


def _required(value: Any, field: str, max_length: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise AssemblyOutboxError(f"{field} is required")
    if len(text) > max_length:
        raise AssemblyOutboxError(f"{field} is too long")
    return text


def _optional_text(value: Any, max_length: int) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) > max_length:
        raise AssemblyOutboxError("optional value is too long")
    return text


def _optional_lower(value: Any, max_length: int) -> str | None:
    text = _optional_text(value, max_length)
    return text.lower() if text is not None else None


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _format_datetime(value: datetime) -> str:
    return _ensure_aware(value).strftime("%Y-%m-%d %H:%M:%S")


def _response_error(response: dict[str, Any]) -> str:
    for key in ("error", "message", "action"):
        value = str(response.get(key) or "").strip()
        if value:
            return value
    return "CRM rejected the event"
