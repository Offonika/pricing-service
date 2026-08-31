from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from xml.etree import ElementTree

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.order_assembly_queue import (
    OrderAssemblyQueueItem,
    OrderAssemblyQueueSyncState,
)

ASSEMBLY_STAGE_ID = "EXECUTING"
CRM_ORDER_NUMBER_FIELD = "UF_CRM_1772784329053"
CRM_DELIVERY_FIELD = "UF_CRM_1772784390536"
CRM_PAYMENT_FIELD = "UF_CRM_1772784357019"
CRM_ASSEMBLY_DUE_AT_FIELD = "UF_CRM_MM_ASSEMBLY_DUE_AT"
CRM_ASSEMBLY_URGENT_FIELD = "UF_CRM_MM_ASSEMBLY_URGENT"
CRM_ASSEMBLY_URGENT_REASON_FIELD = "UF_CRM_MM_ASSEMBLY_URGENT_REASON"
CRM_ASSEMBLY_URGENT_UNTIL_FIELD = "UF_CRM_MM_ASSEMBLY_URGENT_UNTIL"
SYNC_SOURCE = "bitrix_deal"

CRM_SELECT_FIELDS = (
    "ID",
    "STAGE_ID",
    "MOVED_TIME",
    "DATE_MODIFY",
    CRM_ORDER_NUMBER_FIELD,
    CRM_DELIVERY_FIELD,
    CRM_PAYMENT_FIELD,
    CRM_ASSEMBLY_DUE_AT_FIELD,
    CRM_ASSEMBLY_URGENT_FIELD,
    CRM_ASSEMBLY_URGENT_REASON_FIELD,
    CRM_ASSEMBLY_URGENT_UNTIL_FIELD,
)


class AssemblyQueueError(RuntimeError):
    code = "assembly_queue_error"


class AssemblyQueueUpstreamError(AssemblyQueueError):
    code = "bitrix_unavailable"


class AssemblyQueueLimitError(AssemblyQueueError):
    code = "assembly_queue_limit_exceeded"


class AssemblyQueuePayloadError(AssemblyQueueError):
    code = "bitrix_invalid_payload"


@dataclass(frozen=True, slots=True)
class AssemblyQueueSnapshot:
    generated_at: datetime
    last_success_at: datetime
    items: tuple[OrderAssemblyQueueItem, ...]


def sync_assembly_queue(
    session: Session,
    *,
    client: Any,
    limit: int = 500,
    now: datetime | None = None,
) -> AssemblyQueueSnapshot:
    sync_at = _ensure_aware(now or datetime.now(timezone.utc))
    payloads = _fetch_executing_deals(client, limit=limit)
    existing = {
        item.deal_id: item for item in session.scalars(select(OrderAssemblyQueueItem)).all()
    }
    active_deal_ids: set[int] = set()
    items: list[OrderAssemblyQueueItem] = []

    for payload in payloads:
        deal_id = _required_int(payload.get("ID"), field="ID")
        active_deal_ids.add(deal_id)
        item = existing.get(deal_id)
        moved_time = _parse_datetime(payload.get("MOVED_TIME"))
        if moved_time is None:
            moved_time = item.stage_entered_at if item is not None else sync_at

        normalized = _normalized_payload(payload, deal_id=deal_id)
        evidence_id = hashlib.sha256(
            json.dumps(normalized, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        values = {
            "order_number": _clean_string(payload.get(CRM_ORDER_NUMBER_FIELD)),
            "crm_stage": _clean_string(payload.get("STAGE_ID")) or ASSEMBLY_STAGE_ID,
            "stage_entered_at": moved_time,
            "delivery_method": _optional_string(payload.get(CRM_DELIVERY_FIELD)),
            "payment_status": _optional_string(payload.get(CRM_PAYMENT_FIELD)),
            "assembly_due_at": _parse_datetime(payload.get(CRM_ASSEMBLY_DUE_AT_FIELD)),
            "urgent": _bool_value(payload.get(CRM_ASSEMBLY_URGENT_FIELD)),
            "urgent_reason": _optional_string(payload.get(CRM_ASSEMBLY_URGENT_REASON_FIELD)),
            "urgent_until": _parse_datetime(payload.get(CRM_ASSEMBLY_URGENT_UNTIL_FIELD)),
            "synced_at": sync_at,
            "evidence_id": evidence_id,
            "updated_at": sync_at,
        }
        if item is None:
            item = OrderAssemblyQueueItem(deal_id=deal_id, **values)
            session.add(item)
        else:
            for name, value in values.items():
                setattr(item, name, value)
        items.append(item)

    if active_deal_ids:
        session.execute(
            delete(OrderAssemblyQueueItem).where(
                OrderAssemblyQueueItem.deal_id.not_in(active_deal_ids)
            )
        )
    else:
        session.execute(delete(OrderAssemblyQueueItem))

    state = _sync_state(session)
    state.last_success_at = sync_at
    state.last_error_at = None
    state.last_error_code = None
    state.row_count = len(items)
    state.truncated = False
    state.updated_at = sync_at
    session.flush()

    ordered = tuple(sorted(items, key=lambda item: _priority_key(item, now=sync_at)))
    return AssemblyQueueSnapshot(
        generated_at=sync_at,
        last_success_at=sync_at,
        items=ordered,
    )


def record_sync_failure(
    session: Session,
    *,
    error_code: str,
    now: datetime | None = None,
) -> OrderAssemblyQueueSyncState:
    failed_at = _ensure_aware(now or datetime.now(timezone.utc))
    state = _sync_state(session)
    state.last_error_at = failed_at
    state.last_error_code = _clean_string(error_code)[:128]
    state.updated_at = failed_at
    session.flush()
    return state


def get_sync_state(session: Session) -> OrderAssemblyQueueSyncState | None:
    return session.scalar(
        select(OrderAssemblyQueueSyncState).where(OrderAssemblyQueueSyncState.source == SYNC_SOURCE)
    )


def render_queue_xml(snapshot: AssemblyQueueSnapshot) -> bytes:
    root = ElementTree.Element(
        "assembly_queue",
        {
            "generated_at": _format_datetime(snapshot.generated_at),
            "last_success_at": _format_datetime(snapshot.last_success_at),
            "stale": "false",
            "count": str(len(snapshot.items)),
            "truncated": "false",
        },
    )
    for item in snapshot.items:
        node = ElementTree.SubElement(root, "order")
        values = {
            "order_number": item.order_number,
            "deal_id": str(item.deal_id),
            "crm_stage": item.crm_stage,
            "stage_entered_at": _format_datetime(item.stage_entered_at),
            "delivery_method": item.delivery_method or "",
            "payment_status": item.payment_status or "",
            "assembly_due_at": _format_datetime(item.assembly_due_at),
            "urgent": "true" if item.urgent else "false",
            "urgent_reason": item.urgent_reason or "",
            "urgent_until": _format_datetime(item.urgent_until),
            "synced_at": _format_datetime(item.synced_at),
            "evidence_id": item.evidence_id,
        }
        for name, value in values.items():
            child = ElementTree.SubElement(node, name)
            child.text = value
    return ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)


def render_error_xml(
    *,
    code: str,
    last_success_at: datetime | None,
) -> bytes:
    root = ElementTree.Element(
        "assembly_queue_error",
        {
            "code": _clean_string(code),
            "last_success_at": _format_datetime(last_success_at),
            "stale": "true",
        },
    )
    return ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)


def _fetch_executing_deals(client: Any, *, limit: int) -> list[dict[str, Any]]:
    if limit < 1 or limit > 500:
        raise ValueError("limit must be between 1 and 500")
    result: list[dict[str, Any]] = []
    start: int | str = 0
    while True:
        try:
            response = client.call(
                "crm.deal.list",
                {
                    "filter": {"=STAGE_ID": ASSEMBLY_STAGE_ID},
                    "select": list(CRM_SELECT_FIELDS),
                    "order": {"MOVED_TIME": "ASC", "ID": "ASC"},
                    "start": start,
                },
            )
        except Exception as exc:  # noqa: BLE001 - boundary to an external REST client.
            raise AssemblyQueueUpstreamError(type(exc).__name__) from exc
        page = response.get("result")
        if not isinstance(page, list) or not all(isinstance(row, dict) for row in page):
            raise AssemblyQueuePayloadError("crm.deal.list result is not a list")
        result.extend(page)
        if len(result) > limit:
            raise AssemblyQueueLimitError(f"more than {limit} EXECUTING deals")
        next_value = response.get("next")
        if next_value in (None, "", False):
            break
        start = next_value
    return result


def _sync_state(session: Session) -> OrderAssemblyQueueSyncState:
    state = get_sync_state(session)
    if state is None:
        state = OrderAssemblyQueueSyncState(
            source=SYNC_SOURCE,
            row_count=0,
            truncated=False,
        )
        session.add(state)
    return state


def _priority_key(
    item: OrderAssemblyQueueItem,
    *,
    now: datetime,
) -> tuple[int, datetime, datetime, str, int]:
    urgent_is_valid = bool(
        item.urgent
        and item.urgent_reason
        and item.urgent_until is not None
        and _ensure_aware(item.urgent_until) >= now
    )
    maximum = datetime.max.replace(tzinfo=timezone.utc)
    due_at = _ensure_aware(item.assembly_due_at) if item.assembly_due_at else maximum
    stage_entered_at = _ensure_aware(item.stage_entered_at)
    return (
        0 if urgent_is_valid else 1,
        due_at,
        stage_entered_at,
        item.order_number,
        item.deal_id,
    )


def _normalized_payload(payload: dict[str, Any], *, deal_id: int) -> dict[str, Any]:
    return {
        "deal_id": deal_id,
        "stage_id": _clean_string(payload.get("STAGE_ID")),
        "moved_time": _clean_string(payload.get("MOVED_TIME")),
        "date_modify": _clean_string(payload.get("DATE_MODIFY")),
        "order_number": _clean_string(payload.get(CRM_ORDER_NUMBER_FIELD)),
        "delivery_method": _clean_string(payload.get(CRM_DELIVERY_FIELD)),
        "payment_status": _clean_string(payload.get(CRM_PAYMENT_FIELD)),
        "assembly_due_at": _clean_string(payload.get(CRM_ASSEMBLY_DUE_AT_FIELD)),
        "urgent": _bool_value(payload.get(CRM_ASSEMBLY_URGENT_FIELD)),
        "urgent_reason": _clean_string(payload.get(CRM_ASSEMBLY_URGENT_REASON_FIELD)),
        "urgent_until": _clean_string(payload.get(CRM_ASSEMBLY_URGENT_UNTIL_FIELD)),
    }


def _parse_datetime(value: Any) -> datetime | None:
    text = _clean_string(value)
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return _ensure_aware(datetime.fromisoformat(text))
    except ValueError:
        return None


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _format_datetime(value: datetime | None) -> str:
    if value is None:
        return ""
    return _ensure_aware(value).isoformat().replace("+00:00", "Z")


def _required_int(value: Any, *, field: str) -> int:
    try:
        result = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise AssemblyQueuePayloadError(f"invalid {field}") from exc
    if result <= 0:
        raise AssemblyQueuePayloadError(f"invalid {field}")
    return result


def _clean_string(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _optional_string(value: Any) -> str | None:
    result = _clean_string(value)
    return result or None


def _bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return _clean_string(value).casefold() in {"1", "y", "yes", "true", "да"}
