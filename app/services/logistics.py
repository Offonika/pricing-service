from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import Select, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.models import (
    LogisticsDraft,
    LogisticsDraftItem,
    LogisticsDriver,
    LogisticsEventPhoto,
    LogisticsManualReview,
    LogisticsOrderPlan,
    LogisticsOrderPlanUnit,
    LogisticsRouteRun,
    LogisticsRouteRunItem,
    LogisticsTransfer,
    LogisticsTransferEvent,
    LogisticsTransferState,
    LogisticsUser,
    LogisticsWarehouse,
)
from app.services import site_order_fulfillment

ROLE_SENDER = {"sender", "logist", "admin"}
ROLE_RECEIVER = {"receiver", "logist", "admin"}
ROLE_LOGIST = {"logist", "admin"}
SOURCE_CHANNELS = {"bitrix", "telegram", "web_fallback"}

SOURCE_TRANSFER = "transfer"
SOURCE_RTU = "rtu"

FLOW_LEGACY_RTU = "LEGACY_RTU"
FLOW_ORDER_TRANSFER_V1 = "ORDER_TRANSFER_V1"
ORDER_PLAN_SYNC_MAX_AGE_SECONDS = 180

STATUS_AT_WAREHOUSE = "at_warehouse"
STATUS_IN_TRANSIT = "in_transit"
STATUS_WITH_EXTERNAL_CARRIER = "with_external_carrier"

EVENT_SYNCED = "synced"
EVENT_HANDED_TO_DRIVER = "handed_to_driver"
EVENT_ACCEPTED_AT_POINT = "accepted_at_point"
EVENT_HANDED_TO_EXTERNAL_CARRIER = "handed_to_external_carrier"
EVENT_ACCEPTED_FROM_EXTERNAL_CARRIER = "accepted_from_external_carrier"
EVENT_MANUAL_READY_OVERRIDE = "manual_ready_override"
EVENT_HANDOFF_CANCELLED = "handoff_cancelled"
EVENT_RETURNED = "returned"
EVENT_INCIDENT = "incident"

DRAFT_TYPE_HANDOFF = "handoff"
DRAFT_TYPE_RECEIPT = "receipt"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_datetime(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        return value
    normalized = value.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized)


def _same_instant_clock(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _is_fresh_order_plan_sync(value: datetime) -> bool:
    return utcnow() - _same_instant_clock(value) <= timedelta(
        seconds=ORDER_PLAN_SYNC_MAX_AGE_SECONDS
    )


def _http_error(status: int, detail) -> HTTPException:
    return HTTPException(status_code=status, detail=detail)


def _get_actor(session: Session, actor_user_id: int) -> LogisticsUser:
    user = session.get(LogisticsUser, actor_user_id)
    if user is None or not user.is_active:
        raise _http_error(404, "logistics user not found")
    return user


def _require_role(user: LogisticsUser, allowed_roles: set[str]) -> None:
    if user.role not in allowed_roles:
        raise _http_error(403, "user role is not allowed for this operation")


def _get_warehouse(session: Session, warehouse_id: int) -> LogisticsWarehouse:
    warehouse = session.get(LogisticsWarehouse, warehouse_id)
    if warehouse is None or not warehouse.is_active:
        raise _http_error(404, "warehouse not found")
    return warehouse


def _get_driver(session: Session, driver_id: int | None) -> LogisticsDriver | None:
    if driver_id is None:
        return None
    driver = session.get(LogisticsDriver, driver_id)
    if driver is None or not driver.is_active:
        raise _http_error(404, "driver not found")
    return driver


def _normalize_source_document_type(value: str | None) -> str:
    normalized = (value or SOURCE_TRANSFER).strip().lower()
    if normalized not in {SOURCE_TRANSFER, SOURCE_RTU}:
        raise _http_error(422, "unsupported source_document_type")
    return normalized


def _lookup_code_for(item: dict) -> str:
    lookup_code = item.get("lookup_code") or item.get("barcode")
    if not lookup_code:
        raise _http_error(422, "lookup_code or barcode is required")
    return lookup_code


def _create_manual_review(
    session: Session,
    *,
    review_type: str,
    reason: str,
    source_document_type: str | None = None,
    source_external_id: str | None = None,
    transfer_id: int | None = None,
    payload: dict | None = None,
    commit: bool = False,
) -> LogisticsManualReview:
    review = LogisticsManualReview(
        review_type=review_type,
        source_document_type=source_document_type,
        source_external_id=source_external_id,
        transfer_id=transfer_id,
        reason=reason,
        payload=payload,
    )
    session.add(review)
    session.flush()
    if commit:
        session.commit()
    return review


def create_manual_review(
    session: Session,
    *,
    review_type: str,
    reason: str,
    source_document_type: str | None = None,
    source_external_id: str | None = None,
    transfer_id: int | None = None,
    payload: dict | None = None,
    commit: bool = False,
) -> LogisticsManualReview:
    return _create_manual_review(
        session,
        review_type=review_type,
        reason=reason,
        source_document_type=source_document_type,
        source_external_id=source_external_id,
        transfer_id=transfer_id,
        payload=payload,
        commit=commit,
    )


def _get_transfer_by_barcode(session: Session, barcode: str) -> LogisticsTransfer:
    return _get_unit_by_lookup(session, barcode)


def _get_unit_by_lookup(session: Session, code: str) -> LogisticsTransfer:
    code = code.strip()
    if not code:
        raise _http_error(422, "lookup code is empty")
    rows = (
        session.scalars(
            select(LogisticsTransfer)
            .where((LogisticsTransfer.lookup_code == code) | (LogisticsTransfer.barcode == code))
            .options(
                joinedload(LogisticsTransfer.source_warehouse),
                joinedload(LogisticsTransfer.target_warehouse),
                joinedload(LogisticsTransfer.state),
            )
        )
        .unique()
        .all()
    )
    if len(rows) > 1:
        _create_manual_review(
            session,
            review_type="ambiguous_qr",
            reason="lookup code matched more than one logistics unit",
            source_external_id=code,
            payload={"lookup_code": code, "transfer_ids": [row.id for row in rows]},
            commit=True,
        )
        raise _http_error(409, "lookup code is ambiguous")
    if not rows:
        _create_manual_review(
            session,
            review_type="unknown_qr",
            reason="lookup code was not found",
            source_external_id=code,
            payload={"lookup_code": code},
            commit=True,
        )
        raise _http_error(404, "transfer not found by lookup code")
    return rows[0]


def lookup_unit(session: Session, code: str) -> dict:
    transfer = _get_unit_by_lookup(session, code)
    state = _seed_state(session, transfer)
    return {
        "transfer_id": transfer.id,
        "source_document_type": transfer.source_document_type,
        "external_id": transfer.external_id,
        "document_number": transfer.document_number,
        "barcode": transfer.barcode,
        "lookup_code": transfer.lookup_code,
        "site_order_number": transfer.site_order_number,
        "status": state.status,
        "current_warehouse_id": state.current_warehouse_id,
        "dropoff_warehouse_id": state.dropoff_warehouse_id,
        "target_warehouse_id": transfer.target_warehouse_id,
        "document_target_warehouse_id": transfer.document_target_warehouse_id,
        "flow_mode": transfer.flow_mode,
        "plan_key": transfer.plan_key,
        "plan_version": transfer.plan_version,
        "unit_key": transfer.unit_key,
        "expected_unit_count": transfer.expected_unit_count,
        "ready_for_handoff": transfer.ready_for_handoff,
    }


def _get_route_run(session: Session, route_run_id: int | None) -> LogisticsRouteRun | None:
    if route_run_id is None:
        return None
    route_run = session.get(LogisticsRouteRun, route_run_id)
    if route_run is None:
        raise _http_error(404, "route run not found")
    return route_run


def _get_or_create_route_item(
    session: Session,
    *,
    route_run_id: int | None,
    transfer_id: int,
    dropoff_warehouse_id: int | None,
) -> LogisticsRouteRunItem | None:
    if route_run_id is None:
        return None
    route_item = session.scalar(
        select(LogisticsRouteRunItem).where(
            LogisticsRouteRunItem.route_run_id == route_run_id,
            LogisticsRouteRunItem.transfer_id == transfer_id,
        )
    )
    if route_item is None:
        route_item = LogisticsRouteRunItem(
            route_run_id=route_run_id,
            transfer_id=transfer_id,
            dropoff_warehouse_id=dropoff_warehouse_id,
            status="planned",
        )
        session.add(route_item)
        session.flush()
    elif dropoff_warehouse_id is not None:
        route_item.dropoff_warehouse_id = dropoff_warehouse_id
    return route_item


def _complete_route_item(
    session: Session,
    *,
    transfer_id: int,
    warehouse_id: int | None,
    status: str = "completed",
) -> None:
    route_item = session.scalar(
        select(LogisticsRouteRunItem)
        .where(
            LogisticsRouteRunItem.transfer_id == transfer_id,
            LogisticsRouteRunItem.status.in_(["planned", "loaded", "in_transit"]),
        )
        .order_by(LogisticsRouteRunItem.id.desc())
    )
    if route_item is None:
        return
    if warehouse_id is not None and route_item.dropoff_warehouse_id not in (None, warehouse_id):
        return
    route_item.status = status
    route_item.completed_at = utcnow()


def _bridge_rtu_receipt_to_order_fulfillment(
    session: Session,
    *,
    transfer: LogisticsTransfer,
    event: LogisticsTransferEvent,
    warehouse_id: int,
) -> None:
    if transfer.source_document_type != SOURCE_RTU:
        return
    if not transfer.site_order_number:
        return
    if isinstance(transfer.payload, dict) and transfer.payload.get("external_carrier_flow"):
        return
    expected_warehouse_id = transfer.document_target_warehouse_id or transfer.target_warehouse_id
    if warehouse_id != expected_warehouse_id:
        return
    warehouse = session.get(LogisticsWarehouse, warehouse_id)
    driver = session.get(LogisticsDriver, event.driver_id) if event.driver_id is not None else None
    user = session.get(LogisticsUser, event.user_id) if event.user_id is not None else None
    site_order_fulfillment.upsert_execution_event(
        session,
        site_order_number=transfer.site_order_number,
        event_type=site_order_fulfillment.EVENT_PICKUP_STORED,
        event_at=event.event_at,
        source="logistics",
        source_ref=f"logistics_transfer_event:{event.id}",
        confidence="strong",
        raw_message_id=None,
        payload={
            "logistics_transfer_id": transfer.id,
            "source_document_type": transfer.source_document_type,
            "source_external_id": transfer.external_id,
            "warehouse_id": warehouse_id,
            "warehouse_name": warehouse.name if warehouse is not None else None,
            "driver_id": event.driver_id,
            "driver_name": driver.full_name if driver is not None else None,
            "user_id": event.user_id,
            "user_name": user.full_name if user is not None else None,
            "source_channel": event.source,
        },
    )


def _bridge_rtu_handoff_to_order_fulfillment(
    session: Session,
    *,
    transfer: LogisticsTransfer,
    event: LogisticsTransferEvent,
) -> None:
    if transfer.source_document_type != SOURCE_RTU:
        return
    if not transfer.site_order_number:
        return
    if isinstance(transfer.payload, dict) and transfer.payload.get("external_carrier_flow"):
        return
    warehouse = (
        session.get(LogisticsWarehouse, event.warehouse_id)
        if event.warehouse_id is not None
        else None
    )
    dropoff = (
        session.get(LogisticsWarehouse, event.dropoff_warehouse_id)
        if event.dropoff_warehouse_id is not None
        else None
    )
    driver = session.get(LogisticsDriver, event.driver_id) if event.driver_id is not None else None
    user = session.get(LogisticsUser, event.user_id) if event.user_id is not None else None
    site_order_fulfillment.upsert_execution_event(
        session,
        site_order_number=transfer.site_order_number,
        event_type=site_order_fulfillment.EVENT_PICKUP_MOVING,
        event_at=event.event_at,
        source="logistics",
        source_ref=f"logistics_transfer_event:{event.id}",
        confidence="strong",
        raw_message_id=None,
        payload={
            "logistics_transfer_id": transfer.id,
            "source_document_type": transfer.source_document_type,
            "source_external_id": transfer.external_id,
            "warehouse_id": event.warehouse_id,
            "warehouse_name": warehouse.name if warehouse is not None else None,
            "dropoff_warehouse_id": event.dropoff_warehouse_id,
            "dropoff_warehouse_name": dropoff.name if dropoff is not None else None,
            "driver_id": event.driver_id,
            "driver_name": driver.full_name if driver is not None else None,
            "user_id": event.user_id,
            "user_name": user.full_name if user is not None else None,
            "source_channel": event.source,
        },
    )


def _bridge_order_transfer_progress(
    session: Session,
    *,
    transfer: LogisticsTransfer,
    event: LogisticsTransferEvent,
) -> None:
    if not (
        transfer.source_document_type == SOURCE_TRANSFER
        and transfer.flow_mode == FLOW_ORDER_TRANSFER_V1
        and transfer.site_order_number
    ):
        return
    plan_unit = session.scalar(
        select(LogisticsOrderPlanUnit)
        .where(LogisticsOrderPlanUnit.transfer_id == transfer.id)
        .options(
            joinedload(LogisticsOrderPlanUnit.plan),
        )
    )
    if plan_unit is None:
        return
    plan = plan_unit.plan
    if (
        not plan.is_active
        or not _is_fresh_order_plan_sync(plan.synced_at)
        or _has_open_order_flow_conflict(session, plan=plan)
    ):
        return
    # СДЭК/Почта сами двигают CRM. Внутреннее плечо до виртуального терминала
    # фиксируется здесь только как физический аудит и не создаёт второй outbox.
    if _plan_is_external_carrier(plan):
        return
    required_units = [unit for unit in plan.units if unit.is_required]
    if not required_units or len(required_units) != plan.expected_unit_count:
        return
    transfer_ids = [unit.transfer_id for unit in required_units]
    if any(transfer_id is None for transfer_id in transfer_ids):
        return

    if event.event_type == EVENT_HANDED_TO_DRIVER:
        handoff_events = session.scalars(
            select(LogisticsTransferEvent).where(
                LogisticsTransferEvent.transfer_id.in_(transfer_ids),
                LogisticsTransferEvent.event_type == EVENT_HANDED_TO_DRIVER,
            )
        ).all()
        handed_ids = {row.transfer_id for row in handoff_events}
        if len(handed_ids) != len(required_units):
            return
        event_type = site_order_fulfillment.EVENT_PICKUP_MOVING
        event_at = max(
            (row.event_at for row in handoff_events),
            key=_same_instant_clock,
        )
        source_ref = f"logistics_order_plan:{plan.id}:all_handed_off"
    elif event.event_type == EVENT_ACCEPTED_AT_POINT:
        states = [session.get(LogisticsTransferState, transfer_id) for transfer_id in transfer_ids]
        if any(
            state is None
            or state.status != STATUS_AT_WAREHOUSE
            or state.current_warehouse_id != plan.final_warehouse_id
            or state.last_event_type != EVENT_ACCEPTED_AT_POINT
            for state in states
        ):
            return
        event_type = site_order_fulfillment.EVENT_PICKUP_STORED
        event_at = max(
            (state.last_event_at for state in states if state is not None),
            key=_same_instant_clock,
        )
        source_ref = f"logistics_order_plan:{plan.id}:all_accepted_at_final"
    else:
        return

    site_order_fulfillment.upsert_execution_event(
        session,
        site_order_number=transfer.site_order_number,
        event_type=event_type,
        event_at=event_at,
        source="logistics",
        source_ref=source_ref,
        confidence="strong",
        raw_message_id=None,
        payload={
            "origin_order_external_id": plan.origin_order_external_id,
            "flow_mode": plan.flow_mode,
            "plan_key": plan.plan_key,
            "plan_version": plan.plan_version,
            "expected_unit_count": plan.expected_unit_count,
            "completed_unit_count": len(required_units),
            "trigger_transfer_id": transfer.id,
            "trigger_event_id": event.id,
            "source_channel": event.source,
        },
    )


def _logistics_unit_selector(source_document_type: str, external_id: str):
    return select(LogisticsTransfer).where(
        LogisticsTransfer.source_document_type == source_document_type,
        LogisticsTransfer.external_id == external_id,
    )


def _get_transfer_by_legacy_external_id(
    session: Session,
    external_id: str,
) -> LogisticsTransfer | None:
    return session.scalar(
        select(LogisticsTransfer).where(LogisticsTransfer.external_id == external_id)
    )


def _manual_review_for_sync_conflict(
    session: Session,
    *,
    row: LogisticsTransfer,
    item: dict,
    reason: str,
) -> None:
    _create_manual_review(
        session,
        review_type="onec_reconciliation_conflict",
        reason=reason,
        source_document_type=row.source_document_type,
        source_external_id=row.external_id,
        transfer_id=row.id,
        payload={
            "incoming": item,
            "current_status": row.state.status if row.state is not None else None,
            "current_warehouse_id": (
                row.state.current_warehouse_id if row.state is not None else None
            ),
            "dropoff_warehouse_id": (
                row.state.dropoff_warehouse_id if row.state is not None else None
            ),
        },
    )


def _get_transfer_by_barcode_old(session: Session, barcode: str) -> LogisticsTransfer:
    transfer = session.scalar(
        select(LogisticsTransfer)
        .where(LogisticsTransfer.barcode == barcode)
        .options(
            joinedload(LogisticsTransfer.source_warehouse),
            joinedload(LogisticsTransfer.target_warehouse),
            joinedload(LogisticsTransfer.state),
        )
    )
    if transfer is None:
        raise _http_error(404, "transfer not found by barcode")
    return transfer


def _seed_state(session: Session, transfer: LogisticsTransfer) -> LogisticsTransferState:
    state = transfer.state
    if state is None:
        state = LogisticsTransferState(
            transfer_id=transfer.id,
            status=STATUS_AT_WAREHOUSE,
            current_warehouse_id=transfer.source_warehouse_id,
            dropoff_warehouse_id=None,
            driver_id=None,
            last_event_type=EVENT_SYNCED,
            last_event_at=utcnow(),
            last_user_id=None,
            last_document_ref=transfer.document_number,
            version=1,
        )
        session.add(state)
        session.flush()
        transfer.state = state
    return state


def _serialize_draft(draft: LogisticsDraft) -> dict:
    items = []
    for item in draft.items:
        items.append(
            {
                "id": item.id,
                "transfer_id": item.transfer_id,
                "barcode": item.barcode,
                "lookup_code": item.transfer.lookup_code,
                "source_document_type": item.transfer.source_document_type,
                "document_number": item.transfer.document_number,
                "final_recipient_name": item.transfer.final_recipient_name,
                "dropoff_warehouse_id": item.dropoff_warehouse_id,
                "dropoff_warehouse_name": (
                    item.dropoff_warehouse.name if item.dropoff_warehouse is not None else None
                ),
                "scan_at": item.scan_at,
            }
        )
    return {
        "id": draft.id,
        "draft_type": draft.draft_type,
        "status": draft.status,
        "warehouse_id": draft.warehouse_id,
        "driver_id": draft.driver_id,
        "route_run_id": draft.route_run_id,
        "default_dropoff_warehouse_id": draft.default_dropoff_warehouse_id,
        "item_count": len(items),
        "items": items,
    }


def telegram_auth(session: Session, telegram_user_id: int, username: str | None = None) -> dict:
    user = session.scalar(
        select(LogisticsUser)
        .where(LogisticsUser.telegram_user_id == telegram_user_id)
        .options(joinedload(LogisticsUser.default_warehouse))
    )
    if user is None or not user.is_active:
        raise _http_error(404, "telegram user is not mapped to logistics profile")
    if username and user.username != username:
        user.username = username
        session.add(user)
        session.commit()
        session.refresh(user)
    return {
        "id": user.id,
        "external_id": user.external_id,
        "telegram_user_id": user.telegram_user_id,
        "bitrix_user_id": user.bitrix_user_id,
        "username": user.username,
        "full_name": user.full_name,
        "role": user.role,
        "default_warehouse_id": user.default_warehouse_id,
        "default_warehouse_name": (
            user.default_warehouse.name if user.default_warehouse is not None else None
        ),
    }


def _list_open_drafts_for_actor(session: Session, actor_user_id: int) -> list[LogisticsDraft]:
    return session.scalars(
        select(LogisticsDraft)
        .where(
            LogisticsDraft.actor_user_id == actor_user_id,
            LogisticsDraft.status == "open",
        )
        .order_by(LogisticsDraft.id.asc())
    ).all()


def _raise_open_draft_conflict(drafts: list[LogisticsDraft]) -> None:
    if len(drafts) == 1:
        draft = drafts[0]
        raise _http_error(
            409,
            {
                "message": "open draft already exists",
                "draft_id": draft.id,
                "draft_type": draft.draft_type,
            },
        )
    raise _http_error(
        409,
        {
            "message": "multiple open drafts found",
            "draft_ids": [draft.id for draft in drafts],
        },
    )


def create_draft(
    session: Session,
    *,
    draft_type: str,
    actor_user_id: int,
    warehouse_id: int,
    driver_id: int | None = None,
    route_run_id: int | None = None,
    default_dropoff_warehouse_id: int | None = None,
    comment: str | None = None,
) -> dict:
    actor = _get_actor(session, actor_user_id)
    open_drafts = _list_open_drafts_for_actor(session, actor_user_id)
    if open_drafts:
        _raise_open_draft_conflict(open_drafts)

    if draft_type == DRAFT_TYPE_HANDOFF:
        _require_role(actor, ROLE_SENDER)
        if driver_id is None:
            raise _http_error(422, "driver_id is required for handoff draft")
    elif draft_type == DRAFT_TYPE_RECEIPT:
        _require_role(actor, ROLE_RECEIVER)
    else:
        raise _http_error(422, "unsupported draft type")

    _get_warehouse(session, warehouse_id)
    _get_driver(session, driver_id)
    route_run = _get_route_run(session, route_run_id)
    if route_run is not None and route_run.driver_id is not None and driver_id is not None:
        if route_run.driver_id != driver_id:
            raise _http_error(409, "route run driver does not match draft driver")
    if default_dropoff_warehouse_id is not None:
        _get_warehouse(session, default_dropoff_warehouse_id)

    if (
        actor.default_warehouse_id is not None
        and actor.role != "admin"
        and warehouse_id != actor.default_warehouse_id
    ):
        raise _http_error(403, "user cannot operate outside the assigned warehouse")

    draft = LogisticsDraft(
        draft_type=draft_type,
        warehouse_id=warehouse_id,
        actor_user_id=actor_user_id,
        driver_id=driver_id,
        route_run_id=route_run_id,
        default_dropoff_warehouse_id=default_dropoff_warehouse_id,
        comment=comment,
    )
    session.add(draft)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        open_drafts = _list_open_drafts_for_actor(session, actor_user_id)
        if open_drafts:
            _raise_open_draft_conflict(open_drafts)
        raise
    session.refresh(draft)
    return _serialize_draft(draft)


def _get_draft(session: Session, draft_id: int) -> LogisticsDraft:
    draft = session.scalar(
        select(LogisticsDraft)
        .where(LogisticsDraft.id == draft_id)
        .options(
            joinedload(LogisticsDraft.items).joinedload(LogisticsDraftItem.transfer),
            joinedload(LogisticsDraft.items).joinedload(LogisticsDraftItem.dropoff_warehouse),
        )
    )
    if draft is None:
        raise _http_error(404, "draft not found")
    return draft


def _require_ready_for_handoff(
    session: Session,
    transfer: LogisticsTransfer,
) -> None:
    if not (
        transfer.source_document_type == SOURCE_TRANSFER
        and transfer.flow_mode == FLOW_ORDER_TRANSFER_V1
    ):
        return
    plan_unit = session.scalar(
        select(LogisticsOrderPlanUnit)
        .where(LogisticsOrderPlanUnit.transfer_id == transfer.id)
        .options(joinedload(LogisticsOrderPlanUnit.plan))
    )
    if plan_unit is None or not plan_unit.plan.is_active:
        raise _http_error(409, "unit is not linked to the active order plan")
    if _has_open_order_flow_conflict(session, plan=plan_unit.plan):
        raise _http_error(409, "order has an unresolved flow conflict")
    if (
        plan_unit.plan.plan_key != transfer.plan_key
        or plan_unit.plan.plan_version != transfer.plan_version
        or plan_unit.unit_key != transfer.unit_key
    ):
        raise _http_error(409, "unit does not match the active order plan version")
    if not _is_fresh_order_plan_sync(plan_unit.plan.synced_at):
        raise _http_error(409, "order plan sync is stale; handoff is blocked")
    if transfer.onec_deleted:
        raise _http_error(409, "1C marked the transfer as deleted")
    if not transfer.ready_for_handoff or not plan_unit.ready_for_handoff:
        raise _http_error(409, "unit is not posted, printed and assembled for handoff")


def add_scan_to_draft(
    session: Session,
    *,
    draft_id: int,
    actor_user_id: int,
    barcode: str | None = None,
    lookup_code: str | None = None,
    dropoff_warehouse_id: int | None = None,
) -> dict:
    draft = _get_draft(session, draft_id)
    if draft.status != "open":
        raise _http_error(409, "draft is already closed")
    actor = _get_actor(session, actor_user_id)
    if actor.id != draft.actor_user_id and actor.role not in ROLE_LOGIST:
        raise _http_error(403, "user cannot modify this draft")

    scan_code = lookup_code or barcode
    if not scan_code:
        raise _http_error(422, "lookup_code or barcode is required")
    transfer = _get_unit_by_lookup(session, scan_code)
    state = _seed_state(session, transfer)

    existing = session.scalar(
        select(LogisticsDraftItem).where(
            LogisticsDraftItem.draft_id == draft.id,
            LogisticsDraftItem.transfer_id == transfer.id,
        )
    )
    if existing is not None:
        raise _http_error(409, "transfer already added to this draft")

    if draft.draft_type == DRAFT_TYPE_HANDOFF:
        _require_ready_for_handoff(session, transfer)
        if state.status != STATUS_AT_WAREHOUSE or state.current_warehouse_id != draft.warehouse_id:
            if state.status == STATUS_IN_TRANSIT:
                raise _http_error(
                    409,
                    "transfer is already in transit",
                )
            raise _http_error(409, "transfer is not available on sender warehouse")
        target_dropoff = dropoff_warehouse_id or draft.default_dropoff_warehouse_id
        if target_dropoff is None:
            raise _http_error(422, "dropoff warehouse is required for handoff item")
        _get_warehouse(session, target_dropoff)
        route_item = _get_or_create_route_item(
            session,
            route_run_id=draft.route_run_id,
            transfer_id=transfer.id,
            dropoff_warehouse_id=target_dropoff,
        )
        if route_item is not None:
            route_item.status = "planned"
    else:
        if transfer.flow_mode == FLOW_ORDER_TRANSFER_V1:
            plan_unit = session.scalar(
                select(LogisticsOrderPlanUnit)
                .where(LogisticsOrderPlanUnit.transfer_id == transfer.id)
                .options(joinedload(LogisticsOrderPlanUnit.plan))
            )
            if plan_unit is not None and _plan_is_external_carrier(plan_unit.plan):
                raise _http_error(
                    409,
                    "external carrier receipt is confirmed by the existing Bitrix integration",
                )
        if state.status != STATUS_IN_TRANSIT:
            raise _http_error(409, "transfer is already accepted earlier")
        if state.dropoff_warehouse_id != draft.warehouse_id:
            expected = (
                state.dropoff_warehouse.name
                if state.dropoff_warehouse is not None
                else str(state.dropoff_warehouse_id)
            )
            raise _http_error(409, f"expected dropoff warehouse: {expected}")
        target_dropoff = None

    item = LogisticsDraftItem(
        draft_id=draft.id,
        transfer_id=transfer.id,
        barcode=transfer.barcode,
        dropoff_warehouse_id=target_dropoff,
        scan_user_id=actor.id,
        scan_at=utcnow(),
    )
    session.add(item)
    session.commit()
    return _serialize_draft(_get_draft(session, draft_id))


def _attach_photos(event: LogisticsTransferEvent, photos: list[dict]) -> None:
    for photo in photos:
        event.photos.append(
            LogisticsEventPhoto(
                telegram_file_id=photo["telegram_file_id"],
                comment=photo.get("comment"),
            )
        )


def confirm_draft(
    session: Session,
    *,
    draft_id: int,
    actor_user_id: int,
    comment: str | None,
    idempotency_key: str | None,
    photos: list[dict],
    source_channel: str = "telegram",
) -> dict:
    if source_channel not in SOURCE_CHANNELS:
        raise _http_error(422, "unsupported logistics source channel")
    draft = _get_draft(session, draft_id)
    actor = _get_actor(session, actor_user_id)
    if actor.id != draft.actor_user_id and actor.role not in ROLE_LOGIST:
        raise _http_error(403, "user cannot confirm this draft")
    if draft.status == "confirmed":
        return {
            "draft_id": draft.id,
            "status": draft.status,
            "processed_count": len(draft.items),
            "event_type": (
                EVENT_HANDED_TO_DRIVER
                if draft.draft_type == DRAFT_TYPE_HANDOFF
                else EVENT_ACCEPTED_AT_POINT
            ),
        }
    if not draft.items:
        raise _http_error(422, "draft is empty")

    processed_count = 0
    for item in draft.items:
        transfer = session.get(LogisticsTransfer, item.transfer_id)
        state = _seed_state(session, transfer)
        event_key = f"{idempotency_key}:{transfer.id}" if idempotency_key else None

        if draft.draft_type == DRAFT_TYPE_HANDOFF:
            _require_ready_for_handoff(session, transfer)
            if (
                state.status != STATUS_AT_WAREHOUSE
                or state.current_warehouse_id != draft.warehouse_id
            ):
                raise _http_error(409, "transfer is not available for handoff")
            event = LogisticsTransferEvent(
                transfer_id=transfer.id,
                event_type=EVENT_HANDED_TO_DRIVER,
                event_at=utcnow(),
                warehouse_id=draft.warehouse_id,
                dropoff_warehouse_id=item.dropoff_warehouse_id,
                driver_id=draft.driver_id,
                user_id=actor.id,
                comment=comment or draft.comment,
                source=source_channel,
                idempotency_key=event_key,
                document_ref=transfer.document_number,
                meta={"draft_id": draft.id},
            )
            _attach_photos(event, photos)
            session.add(event)
            session.flush()
            route_item = _get_or_create_route_item(
                session,
                route_run_id=draft.route_run_id,
                transfer_id=transfer.id,
                dropoff_warehouse_id=item.dropoff_warehouse_id,
            )
            if route_item is not None:
                route_item.status = "in_transit"
            state.status = STATUS_IN_TRANSIT
            state.current_warehouse_id = None
            state.dropoff_warehouse_id = item.dropoff_warehouse_id
            state.driver_id = draft.driver_id
            state.last_event_type = EVENT_HANDED_TO_DRIVER
        else:
            if (
                state.status != STATUS_IN_TRANSIT
                or state.dropoff_warehouse_id != draft.warehouse_id
            ):
                raise _http_error(409, "transfer is not expected on this warehouse")
            event = LogisticsTransferEvent(
                transfer_id=transfer.id,
                event_type=EVENT_ACCEPTED_AT_POINT,
                event_at=utcnow(),
                warehouse_id=draft.warehouse_id,
                dropoff_warehouse_id=state.dropoff_warehouse_id,
                driver_id=state.driver_id,
                user_id=actor.id,
                comment=comment or draft.comment,
                source=source_channel,
                idempotency_key=event_key,
                document_ref=transfer.document_number,
                meta={"draft_id": draft.id},
            )
            _attach_photos(event, photos)
            session.add(event)
            session.flush()
            _complete_route_item(
                session,
                transfer_id=transfer.id,
                warehouse_id=draft.warehouse_id,
            )
            state.status = STATUS_AT_WAREHOUSE
            state.current_warehouse_id = draft.warehouse_id
            state.dropoff_warehouse_id = None
            state.driver_id = None
            state.last_event_type = EVENT_ACCEPTED_AT_POINT

        state.last_event_at = event.event_at
        state.last_user_id = actor.id
        state.last_document_ref = transfer.document_number
        state.version += 1
        session.flush()
        if draft.draft_type == DRAFT_TYPE_HANDOFF:
            _bridge_rtu_handoff_to_order_fulfillment(
                session,
                transfer=transfer,
                event=event,
            )
        else:
            _bridge_rtu_receipt_to_order_fulfillment(
                session,
                transfer=transfer,
                event=event,
                warehouse_id=draft.warehouse_id,
            )
        _bridge_order_transfer_progress(
            session,
            transfer=transfer,
            event=event,
        )
        processed_count += 1

    draft.status = "confirmed"
    draft.confirmed_at = utcnow()
    draft.comment = comment or draft.comment
    session.commit()
    return {
        "draft_id": draft.id,
        "status": draft.status,
        "processed_count": processed_count,
        "event_type": (
            EVENT_HANDED_TO_DRIVER
            if draft.draft_type == DRAFT_TYPE_HANDOFF
            else EVENT_ACCEPTED_AT_POINT
        ),
    }


def list_expected_deliveries(
    session: Session,
    *,
    warehouse_id: int | None,
    driver_id: int | None = None,
) -> list[dict]:
    stmt: Select[tuple[LogisticsTransferState]] = (
        select(LogisticsTransferState)
        .where(
            LogisticsTransferState.status == STATUS_IN_TRANSIT,
        )
        .options(
            joinedload(LogisticsTransferState.transfer).joinedload(
                LogisticsTransfer.source_warehouse
            ),
            joinedload(LogisticsTransferState.transfer).joinedload(
                LogisticsTransfer.target_warehouse
            ),
            joinedload(LogisticsTransferState.dropoff_warehouse),
            joinedload(LogisticsTransferState.driver),
        )
        .order_by(LogisticsTransferState.last_event_at.desc())
    )
    if warehouse_id is not None:
        stmt = stmt.where(LogisticsTransferState.dropoff_warehouse_id == warehouse_id)
    if driver_id is not None:
        stmt = stmt.where(LogisticsTransferState.driver_id == driver_id)
    rows = session.scalars(stmt).all()
    payload = []
    for state in rows:
        transfer = state.transfer
        payload.append(
            {
                "transfer_id": transfer.id,
                "external_id": transfer.external_id,
                "source_document_type": transfer.source_document_type,
                "document_number": transfer.document_number,
                "barcode": transfer.barcode,
                "lookup_code": transfer.lookup_code,
                "site_order_number": transfer.site_order_number,
                "source_warehouse_name": transfer.source_warehouse.name,
                "target_warehouse_name": transfer.target_warehouse.name,
                "final_recipient_name": transfer.final_recipient_name,
                "driver_name": state.driver.full_name if state.driver is not None else None,
                "dropoff_warehouse_name": (
                    state.dropoff_warehouse.name if state.dropoff_warehouse is not None else None
                ),
                "last_event_type": state.last_event_type,
                "last_event_at": state.last_event_at,
            }
        )
    return payload


def list_rtu_ready_for_pickup(
    session: Session,
    *,
    warehouse_code: str,
    date_from: date | None = None,
) -> list[dict]:
    normalized_code = warehouse_code.strip().casefold()
    if not normalized_code:
        raise _http_error(422, "warehouse_code is required")

    warehouses = [
        warehouse
        for warehouse in session.scalars(
            select(LogisticsWarehouse)
            .where(LogisticsWarehouse.is_active.is_(True))
            .order_by(LogisticsWarehouse.id.asc())
        ).all()
        if normalized_code in _warehouse_lookup_codes(warehouse)
    ]
    if not warehouses:
        raise _http_error(404, "warehouse not found")
    if len(warehouses) > 1:
        raise _http_error(409, "warehouse_code matched multiple warehouses")

    warehouse = warehouses[0]
    stmt: Select[tuple[LogisticsTransferState]] = (
        select(LogisticsTransferState)
        .join(LogisticsTransfer, LogisticsTransfer.id == LogisticsTransferState.transfer_id)
        .where(
            LogisticsTransfer.source_document_type == SOURCE_RTU,
            LogisticsTransfer.target_warehouse_id == warehouse.id,
            LogisticsTransfer.source_warehouse_id != warehouse.id,
            LogisticsTransferState.status == STATUS_AT_WAREHOUSE,
            LogisticsTransferState.current_warehouse_id == warehouse.id,
            LogisticsTransferState.last_event_type == EVENT_ACCEPTED_AT_POINT,
        )
        .options(joinedload(LogisticsTransferState.transfer))
        .order_by(
            LogisticsTransfer.document_date.asc(),
            LogisticsTransfer.document_number.asc(),
        )
    )
    if date_from is not None:
        stmt = stmt.where(LogisticsTransfer.document_date >= datetime.combine(date_from, time.min))

    return [
        {
            "external_id": state.transfer.external_id,
            "document_number": state.transfer.document_number,
            "document_date": state.transfer.document_date,
            "accepted_at": state.last_event_at,
        }
        for state in session.scalars(stmt).all()
    ]


def list_orders_ready_for_pickup(
    session: Session,
    *,
    warehouse_code: str,
    date_from: date | None = None,
) -> list[dict]:
    normalized_code = warehouse_code.strip().casefold()
    if not normalized_code:
        raise _http_error(422, "warehouse_code is required")
    warehouses = [
        warehouse
        for warehouse in session.scalars(
            select(LogisticsWarehouse)
            .where(LogisticsWarehouse.is_active.is_(True))
            .order_by(LogisticsWarehouse.id.asc())
        ).all()
        if normalized_code in _warehouse_lookup_codes(warehouse)
    ]
    if not warehouses:
        raise _http_error(404, "warehouse not found")
    if len(warehouses) > 1:
        raise _http_error(409, "warehouse_code matched multiple warehouses")
    warehouse = warehouses[0]

    ready_after = datetime.combine(date_from, time.min) if date_from is not None else None
    results: list[dict] = []

    legacy_states = session.scalars(
        select(LogisticsTransferState)
        .join(LogisticsTransfer, LogisticsTransfer.id == LogisticsTransferState.transfer_id)
        .where(
            LogisticsTransfer.source_document_type == SOURCE_RTU,
            LogisticsTransfer.target_warehouse_id == warehouse.id,
            LogisticsTransfer.source_warehouse_id != warehouse.id,
            LogisticsTransferState.status == STATUS_AT_WAREHOUSE,
            LogisticsTransferState.current_warehouse_id == warehouse.id,
            LogisticsTransferState.last_event_type == EVENT_ACCEPTED_AT_POINT,
        )
        .options(joinedload(LogisticsTransferState.transfer))
    ).all()
    legacy_groups: dict[str, list[LogisticsTransferState]] = {}
    for state in legacy_states:
        transfer = state.transfer
        payload = transfer.payload if isinstance(transfer.payload, dict) else {}
        if transfer.flow_mode == FLOW_ORDER_TRANSFER_V1 or payload.get("final_sale"):
            continue
        group_key = (
            transfer.origin_order_external_id
            or transfer.site_order_number
            or f"rtu:{transfer.external_id}"
        )
        legacy_groups.setdefault(group_key, []).append(state)
    for group_key, states in legacy_groups.items():
        ready_at = max(state.last_event_at for state in states)
        if ready_after is not None and ready_at < ready_after:
            continue
        transfers = [state.transfer for state in states]
        results.append(
            {
                "origin_order_external_id": (transfers[0].origin_order_external_id or group_key),
                "site_order_number": transfers[0].site_order_number,
                "flow_mode": FLOW_LEGACY_RTU,
                "plan_key": None,
                "plan_version": None,
                "ready_at": ready_at,
                "expected_unit_count": len(transfers),
                "accepted_unit_count": len(transfers),
                "source_external_ids": sorted(transfer.external_id for transfer in transfers),
            }
        )

    plans = (
        session.scalars(
            select(LogisticsOrderPlan)
            .where(
                LogisticsOrderPlan.is_active.is_(True),
                LogisticsOrderPlan.flow_mode == FLOW_ORDER_TRANSFER_V1,
                LogisticsOrderPlan.final_warehouse_id == warehouse.id,
            )
            .options(
                joinedload(LogisticsOrderPlan.units)
                .joinedload(LogisticsOrderPlanUnit.transfer)
                .joinedload(LogisticsTransfer.state)
            )
        )
        .unique()
        .all()
    )
    for plan in plans:
        if _plan_is_external_carrier(plan):
            continue
        if _has_open_order_flow_conflict(session, plan=plan):
            continue
        required_units = [unit for unit in plan.units if unit.is_required]
        if not required_units or len(required_units) != plan.expected_unit_count:
            continue
        accepted_units = [
            unit
            for unit in required_units
            if unit.transfer is not None
            and unit.transfer.state is not None
            and unit.transfer.state.status == STATUS_AT_WAREHOUSE
            and unit.transfer.state.current_warehouse_id == plan.final_warehouse_id
            and unit.transfer.state.last_event_type == EVENT_ACCEPTED_AT_POINT
        ]
        if len(accepted_units) != len(required_units):
            continue
        ready_at = max(unit.transfer.state.last_event_at for unit in accepted_units)
        if ready_after is not None and ready_at < ready_after:
            continue
        results.append(
            {
                "origin_order_external_id": plan.origin_order_external_id,
                "site_order_number": plan.site_order_number,
                "flow_mode": plan.flow_mode,
                "plan_key": plan.plan_key,
                "plan_version": plan.plan_version,
                "ready_at": ready_at,
                "expected_unit_count": plan.expected_unit_count,
                "accepted_unit_count": len(accepted_units),
                "source_external_ids": sorted(unit.transfer.external_id for unit in accepted_units),
            }
        )
    return sorted(
        results,
        key=lambda row: (row["ready_at"], row["origin_order_external_id"]),
    )


def _warehouse_lookup_codes(warehouse: LogisticsWarehouse) -> set[str]:
    values: set[str] = {warehouse.external_id.strip().casefold()}
    payload = warehouse.payload if isinstance(warehouse.payload, dict) else {}
    direct_code = payload.get("code")
    if direct_code:
        values.add(str(direct_code).strip().casefold())
    departments = payload.get("onec_departments")
    if isinstance(departments, list):
        for department in departments:
            if not isinstance(department, dict):
                continue
            code = department.get("code")
            if code:
                values.add(str(code).strip().casefold())
    return {value for value in values if value}


def list_monitor(
    session: Session,
    *,
    status: str | None = None,
    warehouse_id: int | None = None,
    driver_id: int | None = None,
    final_recipient: str | None = None,
    source_document_type: str | None = None,
    route_run_id: int | None = None,
    with_external_carrier: bool | None = None,
    manual_review: bool | None = None,
) -> list[dict]:
    stmt = (
        select(LogisticsTransfer)
        .options(
            joinedload(LogisticsTransfer.source_warehouse),
            joinedload(LogisticsTransfer.target_warehouse),
            joinedload(LogisticsTransfer.state).joinedload(
                LogisticsTransferState.current_warehouse
            ),
            joinedload(LogisticsTransfer.state).joinedload(
                LogisticsTransferState.dropoff_warehouse
            ),
            joinedload(LogisticsTransfer.state).joinedload(LogisticsTransferState.driver),
            joinedload(LogisticsTransfer.state).joinedload(LogisticsTransferState.last_user),
        )
        .order_by(LogisticsTransfer.document_date.desc())
    )
    rows = session.scalars(stmt).all()
    transfer_ids = [row.id for row in rows]
    route_items_by_transfer: dict[int, LogisticsRouteRunItem] = {}
    if transfer_ids:
        route_items = (
            session.scalars(
                select(LogisticsRouteRunItem)
                .where(LogisticsRouteRunItem.transfer_id.in_(transfer_ids))
                .options(joinedload(LogisticsRouteRunItem.route_run))
                .order_by(LogisticsRouteRunItem.id.desc())
            )
            .unique()
            .all()
        )
        for route_item in route_items:
            route_items_by_transfer.setdefault(route_item.transfer_id, route_item)
    manual_review_counts: dict[int, int] = {}
    if transfer_ids:
        reviews = session.scalars(
            select(LogisticsManualReview).where(
                LogisticsManualReview.transfer_id.in_(transfer_ids),
                LogisticsManualReview.status == "open",
            )
        ).all()
        for review in reviews:
            if review.transfer_id is not None:
                manual_review_counts[review.transfer_id] = (
                    manual_review_counts.get(review.transfer_id, 0) + 1
                )
    payload = []
    driver_filter = driver_id
    for transfer in rows:
        state = transfer.state
        if state is None:
            current_warehouse_id = transfer.source_warehouse_id
            dropoff_warehouse_id = None
            state_driver_id = None
            status_value = STATUS_AT_WAREHOUSE
            current_warehouse_name = transfer.source_warehouse.name
            dropoff_warehouse_name = None
            driver_name = None
            last_event_type = EVENT_SYNCED
            last_event_at = transfer.created_at
            last_user_name = None
        else:
            current_warehouse_id = state.current_warehouse_id
            dropoff_warehouse_id = state.dropoff_warehouse_id
            state_driver_id = state.driver_id
            status_value = state.status
            current_warehouse_name = (
                state.current_warehouse.name if state.current_warehouse is not None else None
            )
            dropoff_warehouse_name = (
                state.dropoff_warehouse.name if state.dropoff_warehouse is not None else None
            )
            driver_name = state.driver.full_name if state.driver is not None else None
            last_event_type = state.last_event_type
            last_event_at = state.last_event_at
            last_user_name = state.last_user.full_name if state.last_user is not None else None
        if status and status_value != status:
            continue
        if source_document_type and transfer.source_document_type != source_document_type:
            continue
        if (
            with_external_carrier is not None
            and (status_value == STATUS_WITH_EXTERNAL_CARRIER) is not with_external_carrier
        ):
            continue
        if (
            warehouse_id
            and current_warehouse_id != warehouse_id
            and dropoff_warehouse_id != warehouse_id
        ):
            continue
        if driver_filter and state_driver_id != driver_filter:
            continue
        if (
            final_recipient
            and final_recipient.lower() not in (transfer.final_recipient_name or "").lower()
        ):
            continue
        route_item = route_items_by_transfer.get(transfer.id)
        if route_run_id is not None and (
            route_item is None or route_item.route_run_id != route_run_id
        ):
            continue
        review_count = manual_review_counts.get(transfer.id, 0)
        if manual_review is not None and (review_count > 0) is not manual_review:
            continue
        payload.append(
            {
                "transfer_id": transfer.id,
                "external_id": transfer.external_id,
                "source_document_type": transfer.source_document_type,
                "document_number": transfer.document_number,
                "document_date": transfer.document_date,
                "barcode": transfer.barcode,
                "lookup_code": transfer.lookup_code,
                "site_order_number": transfer.site_order_number,
                "source_warehouse_name": transfer.source_warehouse.name,
                "target_warehouse_name": transfer.target_warehouse.name,
                "final_recipient_name": transfer.final_recipient_name,
                "status": status_value,
                "current_warehouse_name": current_warehouse_name,
                "dropoff_warehouse_name": dropoff_warehouse_name,
                "driver_name": driver_name,
                "last_event_type": last_event_type,
                "last_event_at": last_event_at,
                "last_user_name": last_user_name,
                "route_run_id": route_item.route_run_id if route_item is not None else None,
                "route_name": (
                    route_item.route_run.route_name
                    if route_item is not None and route_item.route_run is not None
                    else None
                ),
                "manual_review_count": review_count,
            }
        )
    return payload


def get_transfer_history(session: Session, transfer_id: int) -> list[dict]:
    events = (
        session.execute(
            select(LogisticsTransferEvent)
            .where(LogisticsTransferEvent.transfer_id == transfer_id)
            .options(
                joinedload(LogisticsTransferEvent.warehouse),
                joinedload(LogisticsTransferEvent.dropoff_warehouse),
                joinedload(LogisticsTransferEvent.driver),
                joinedload(LogisticsTransferEvent.user),
                joinedload(LogisticsTransferEvent.photos),
            )
            .order_by(LogisticsTransferEvent.event_at.desc())
        )
        .unique()
        .scalars()
        .all()
    )
    return [
        {
            "id": event.id,
            "event_type": event.event_type,
            "event_at": event.event_at,
            "warehouse_name": event.warehouse.name if event.warehouse is not None else None,
            "dropoff_warehouse_name": (
                event.dropoff_warehouse.name if event.dropoff_warehouse is not None else None
            ),
            "driver_name": event.driver.full_name if event.driver is not None else None,
            "user_name": event.user.full_name if event.user is not None else None,
            "comment": event.comment,
            "source": event.source,
            "photos": [
                {"telegram_file_id": photo.telegram_file_id, "comment": photo.comment}
                for photo in event.photos
            ],
        }
        for event in events
    ]


def create_transfer_event(
    session: Session,
    *,
    transfer_id: int,
    actor_user_id: int,
    event_type: str,
    source: str,
    warehouse_id: int | None = None,
    comment: str | None = None,
    idempotency_key: str | None = None,
    photos: list[dict] | None = None,
) -> dict:
    actor = _get_actor(session, actor_user_id)
    transfer = session.get(LogisticsTransfer, transfer_id)
    if transfer is None:
        raise _http_error(404, "transfer not found")
    state = _seed_state(session, transfer)
    event_key = f"{idempotency_key}:{transfer_id}:{event_type}" if idempotency_key else None
    if event_key is not None:
        existing = session.scalar(
            select(LogisticsTransferEvent).where(
                LogisticsTransferEvent.idempotency_key == event_key
            )
        )
        if existing is not None:
            return {"status": "ok"}

    if event_type == EVENT_RETURNED:
        _require_role(actor, ROLE_SENDER | ROLE_RECEIVER | ROLE_LOGIST)
        if warehouse_id is None:
            raise _http_error(422, "warehouse_id is required for returned event")
        _get_warehouse(session, warehouse_id)
        state.status = STATUS_AT_WAREHOUSE
        state.current_warehouse_id = warehouse_id
        state.dropoff_warehouse_id = None
        state.driver_id = None
    elif event_type == EVENT_HANDOFF_CANCELLED:
        _require_role(actor, ROLE_SENDER | ROLE_LOGIST)
        if warehouse_id is None:
            raise _http_error(422, "warehouse_id is required for handoff cancellation")
        _get_warehouse(session, warehouse_id)
        state.status = STATUS_AT_WAREHOUSE
        state.current_warehouse_id = warehouse_id
        state.dropoff_warehouse_id = None
        state.driver_id = None
    elif event_type == EVENT_INCIDENT:
        _require_role(actor, ROLE_SENDER | ROLE_RECEIVER | ROLE_LOGIST)
        pass
    else:
        raise _http_error(422, "unsupported event type")

    event = LogisticsTransferEvent(
        transfer_id=transfer_id,
        event_type=event_type,
        event_at=utcnow(),
        warehouse_id=warehouse_id,
        dropoff_warehouse_id=state.dropoff_warehouse_id,
        driver_id=state.driver_id,
        user_id=actor.id,
        comment=comment,
        source=source,
        idempotency_key=event_key,
        document_ref=transfer.document_number,
        meta=None,
    )
    _attach_photos(event, photos or [])
    session.add(event)

    state.last_event_type = event_type
    state.last_event_at = event.event_at
    state.last_user_id = actor.id
    state.last_document_ref = transfer.document_number
    state.version += 1
    session.commit()
    return {"status": "ok"}


@dataclass
class _SyncCounters:
    created: int = 0
    updated: int = 0


def sync_warehouses(session: Session, items: list[dict]) -> dict:
    counters = _SyncCounters()
    for item in items:
        row = session.scalar(
            select(LogisticsWarehouse).where(LogisticsWarehouse.external_id == item["external_id"])
        )
        if row is None:
            row = LogisticsWarehouse(
                external_id=item["external_id"],
                name=item["name"],
                kind=item.get("kind", "store"),
                payload=item.get("payload"),
                is_active=item.get("is_active", True),
            )
            session.add(row)
            counters.created += 1
        else:
            row.name = item["name"]
            row.kind = item.get("kind", row.kind)
            row.payload = item.get("payload", row.payload)
            row.is_active = item.get("is_active", row.is_active)
            counters.updated += 1
    session.commit()
    return counters.__dict__


def sync_drivers(session: Session, items: list[dict]) -> dict:
    counters = _SyncCounters()
    for item in items:
        row = None
        if item.get("external_id"):
            row = session.scalar(
                select(LogisticsDriver).where(LogisticsDriver.external_id == item["external_id"])
            )
        if row is None:
            row = session.scalar(
                select(LogisticsDriver).where(LogisticsDriver.full_name == item["full_name"])
            )
        if row is None:
            row = LogisticsDriver(
                external_id=item.get("external_id"),
                full_name=item["full_name"],
                phone=item.get("phone"),
                is_active=item.get("is_active", True),
            )
            session.add(row)
            counters.created += 1
        else:
            row.external_id = item.get("external_id") or row.external_id
            row.full_name = item["full_name"]
            row.phone = item.get("phone")
            row.is_active = item.get("is_active", row.is_active)
            counters.updated += 1
    session.commit()
    return counters.__dict__


def sync_users(session: Session, items: list[dict]) -> dict:
    counters = _SyncCounters()
    warehouses = {
        row.external_id: row.id for row in session.scalars(select(LogisticsWarehouse)).all()
    }
    for item in items:
        row = None
        if item.get("external_id"):
            row = session.scalar(
                select(LogisticsUser).where(LogisticsUser.external_id == item["external_id"])
            )
        if row is None and item.get("telegram_user_id") is not None:
            row = session.scalar(
                select(LogisticsUser).where(
                    LogisticsUser.telegram_user_id == item["telegram_user_id"]
                )
            )
        if row is None and item.get("bitrix_user_id"):
            row = session.scalar(
                select(LogisticsUser).where(LogisticsUser.bitrix_user_id == item["bitrix_user_id"])
            )
        warehouse_id = None
        if item.get("default_warehouse_external_id") is not None:
            warehouse_id = warehouses.get(item["default_warehouse_external_id"])
            if warehouse_id is None:
                raise _http_error(422, "user references unknown default warehouse")
        if row is None:
            row = LogisticsUser(
                external_id=item.get("external_id"),
                telegram_user_id=item.get("telegram_user_id"),
                bitrix_user_id=item.get("bitrix_user_id"),
                username=item.get("username"),
                full_name=item["full_name"],
                role=item["role"],
                default_warehouse_id=warehouse_id,
                is_active=item.get("is_active", True),
            )
            session.add(row)
            counters.created += 1
        else:
            row.telegram_user_id = item.get("telegram_user_id", row.telegram_user_id)
            row.bitrix_user_id = item.get("bitrix_user_id", row.bitrix_user_id)
            row.username = item.get("username", row.username)
            row.full_name = item["full_name"]
            row.role = item["role"]
            row.default_warehouse_id = warehouse_id
            row.is_active = item.get("is_active", row.is_active)
            counters.updated += 1
    session.commit()
    return counters.__dict__


def _plan_has_started(plan: LogisticsOrderPlan) -> bool:
    for unit in plan.units:
        transfer = unit.transfer
        state = transfer.state if transfer is not None else None
        if state is None:
            continue
        if state.last_event_type != EVENT_SYNCED or state.status != STATUS_AT_WAREHOUSE:
            return True
    return False


def _plan_is_external_carrier(plan: LogisticsOrderPlan) -> bool:
    payload = plan.payload if isinstance(plan.payload, dict) else {}
    return bool(payload.get("external_carrier"))


def _plan_required_units_at_final(plan: LogisticsOrderPlan) -> bool:
    required_units = [unit for unit in plan.units if unit.is_required]
    if not required_units or len(required_units) != plan.expected_unit_count:
        return False
    for unit in required_units:
        transfer = unit.transfer
        state = transfer.state if transfer is not None else None
        if (
            state is None
            or state.status != STATUS_AT_WAREHOUSE
            or state.current_warehouse_id != plan.final_warehouse_id
            or state.last_event_type
            not in {EVENT_ACCEPTED_AT_POINT, EVENT_ACCEPTED_FROM_EXTERNAL_CARRIER}
        ):
            return False
    return True


def order_transfer_rtu_disposition(
    session: Session,
    *,
    origin_order_external_id: str | None,
    site_order_number: str | None,
) -> tuple[str, LogisticsOrderPlan | None]:
    """Classify an unmarked RTU received through the legacy polling contract."""

    plan = _active_order_plan(
        session,
        origin_order_external_id=origin_order_external_id,
        site_order_number=site_order_number,
    )
    if plan is None:
        return "legacy", None
    if _plan_required_units_at_final(plan):
        return "final_sale", plan
    return "conflict", plan


def _active_order_plan(
    session: Session,
    *,
    origin_order_external_id: str | None,
    site_order_number: str | None,
) -> LogisticsOrderPlan | None:
    conditions = []
    if origin_order_external_id:
        conditions.append(LogisticsOrderPlan.origin_order_external_id == origin_order_external_id)
    if site_order_number:
        conditions.append(LogisticsOrderPlan.site_order_number == site_order_number)
    if not conditions:
        return None
    selector = select(LogisticsOrderPlan).where(LogisticsOrderPlan.is_active.is_(True))
    selector = selector.where(
        conditions[0] if len(conditions) == 1 else conditions[0] | conditions[1]
    )
    return (
        session.execute(
            selector.options(
                joinedload(LogisticsOrderPlan.units)
                .joinedload(LogisticsOrderPlanUnit.transfer)
                .joinedload(LogisticsTransfer.state)
            )
        )
        .unique()
        .scalar_one_or_none()
    )


def _legacy_rtu_for_order(
    session: Session,
    *,
    origin_order_external_id: str | None,
    site_order_number: str | None,
) -> LogisticsTransfer | None:
    conditions = []
    if origin_order_external_id:
        conditions.append(LogisticsTransfer.origin_order_external_id == origin_order_external_id)
    if site_order_number:
        conditions.append(LogisticsTransfer.site_order_number == site_order_number)
    if not conditions:
        return None
    selector = select(LogisticsTransfer).where(LogisticsTransfer.source_document_type == SOURCE_RTU)
    selector = selector.where(
        conditions[0] if len(conditions) == 1 else conditions[0] | conditions[1]
    )
    for transfer in session.scalars(selector).all():
        payload = transfer.payload if isinstance(transfer.payload, dict) else {}
        if transfer.flow_mode == FLOW_ORDER_TRANSFER_V1 or payload.get("final_sale"):
            continue
        return transfer
    return None


def _create_order_flow_conflict(
    session: Session,
    *,
    origin_order_external_id: str | None,
    site_order_number: str | None,
    reason: str,
    payload: dict,
) -> None:
    source_external_id = origin_order_external_id or site_order_number
    existing = session.scalar(
        select(LogisticsManualReview).where(
            LogisticsManualReview.review_type == "order_flow_conflict",
            LogisticsManualReview.status == "open",
            LogisticsManualReview.source_external_id == source_external_id,
        )
    )
    if existing is None:
        _create_manual_review(
            session,
            review_type="order_flow_conflict",
            reason=reason,
            source_document_type="customer_order",
            source_external_id=source_external_id,
            payload=payload,
        )


def _has_open_order_flow_conflict(
    session: Session,
    *,
    plan: LogisticsOrderPlan,
) -> bool:
    references = [plan.origin_order_external_id]
    if plan.site_order_number:
        references.append(plan.site_order_number)
    return (
        session.scalar(
            select(LogisticsManualReview.id).where(
                LogisticsManualReview.review_type == "order_flow_conflict",
                LogisticsManualReview.status == "open",
                LogisticsManualReview.source_external_id.in_(references),
            )
        )
        is not None
    )


def _carrier_confirmation_plan(session: Session, item: dict) -> LogisticsOrderPlan | None:
    origin_order_external_id = item.get("origin_order_external_id")
    site_order_number = item.get("site_order_number")
    conditions = []
    if origin_order_external_id:
        conditions.append(LogisticsOrderPlan.origin_order_external_id == origin_order_external_id)
    if site_order_number:
        conditions.append(LogisticsOrderPlan.site_order_number == site_order_number)
    if not conditions:
        return None
    selector = select(LogisticsOrderPlan).where(
        LogisticsOrderPlan.is_active.is_(True),
        LogisticsOrderPlan.flow_mode == FLOW_ORDER_TRANSFER_V1,
    )
    selector = selector.where(
        conditions[0] if len(conditions) == 1 else conditions[0] | conditions[1]
    )
    candidates = session.scalars(selector).all()
    exact = [
        plan
        for plan in candidates
        if (
            not origin_order_external_id
            or plan.origin_order_external_id == origin_order_external_id
        )
        and (not site_order_number or plan.site_order_number == site_order_number)
    ]
    if len(exact) == 1 and len(candidates) == 1:
        return exact[0]
    if candidates:
        _create_order_flow_conflict(
            session,
            origin_order_external_id=origin_order_external_id,
            site_order_number=site_order_number,
            reason="carrier confirmation has an ambiguous order link",
            payload={
                "confirmation": item,
                "candidate_plan_ids": [plan.id for plan in candidates],
            },
        )
        session.commit()
        raise _http_error(409, "carrier confirmation order link is ambiguous")
    return None


def sync_order_plans(session: Session, items: list[dict]) -> dict:
    counters = _SyncCounters()
    warehouses = {row.external_id: row for row in session.scalars(select(LogisticsWarehouse)).all()}
    for item in items:
        origin_order_external_id = item["origin_order_external_id"].strip()
        plan_key = item["plan_key"].strip()
        plan_version = item["plan_version"]
        if not origin_order_external_id or not plan_key:
            raise _http_error(422, "order plan requires non-empty order and plan keys")
        if item.get("flow_mode") != FLOW_ORDER_TRANSFER_V1:
            raise _http_error(422, "order plan supports only ORDER_TRANSFER_V1")

        final_warehouse = warehouses.get(item["final_warehouse_external_id"])
        if final_warehouse is None:
            raise _http_error(422, "order plan references unknown final warehouse")
        required_count = sum(1 for unit in item.get("units", []) if unit.get("is_required", True))
        if required_count != item["expected_unit_count"]:
            raise _http_error(422, "order plan expected_unit_count mismatch")

        legacy_rtu = _legacy_rtu_for_order(
            session,
            origin_order_external_id=origin_order_external_id,
            site_order_number=item.get("site_order_number"),
        )
        if legacy_rtu is not None:
            _create_order_flow_conflict(
                session,
                origin_order_external_id=origin_order_external_id,
                site_order_number=item.get("site_order_number"),
                reason="legacy RTU and ORDER_TRANSFER_V1 plan exist for the same order",
                payload={"plan": item, "legacy_rtu_external_id": legacy_rtu.external_id},
            )
            session.commit()
            raise _http_error(409, "order has both legacy RTU and transfer plan")

        current = _active_order_plan(
            session,
            origin_order_external_id=origin_order_external_id,
            site_order_number=None,
        )
        row = (
            session.execute(
                select(LogisticsOrderPlan)
                .where(
                    LogisticsOrderPlan.plan_key == plan_key,
                    LogisticsOrderPlan.plan_version == plan_version,
                )
                .options(
                    joinedload(LogisticsOrderPlan.units)
                    .joinedload(LogisticsOrderPlanUnit.transfer)
                    .joinedload(LogisticsTransfer.state)
                )
            )
            .unique()
            .scalar_one_or_none()
        )

        incoming_shape = {
            (
                unit["unit_key"],
                unit["source_warehouse_external_id"],
                unit["target_warehouse_external_id"],
                unit.get("is_required", True),
            )
            for unit in item.get("units", [])
        }
        if current is not None:
            current_shape = {
                (
                    unit.unit_key,
                    unit.source_warehouse.external_id,
                    unit.target_warehouse.external_id,
                    unit.is_required,
                )
                for unit in current.units
            }
            changes_started_plan = _plan_has_started(current) and (
                current.plan_version != plan_version
                or current.plan_key != plan_key
                or current_shape != incoming_shape
                or current.final_warehouse_id != final_warehouse.id
            )
            if changes_started_plan:
                _create_order_flow_conflict(
                    session,
                    origin_order_external_id=origin_order_external_id,
                    site_order_number=item.get("site_order_number"),
                    reason="1C changed an ORDER_TRANSFER_V1 plan after logistics handoff",
                    payload={
                        "incoming_plan": item,
                        "active_plan_key": current.plan_key,
                        "active_plan_version": current.plan_version,
                    },
                )
                session.commit()
                raise _http_error(409, "started order plan can only be changed manually")
            if current is not row:
                current.is_active = False
                session.flush()

        sync_time = utcnow()
        if row is None:
            row = LogisticsOrderPlan(
                origin_order_external_id=origin_order_external_id,
                site_order_number=item.get("site_order_number"),
                flow_mode=FLOW_ORDER_TRANSFER_V1,
                plan_key=plan_key,
                plan_version=plan_version,
                final_warehouse_id=final_warehouse.id,
                status=item.get("status") or "planned",
                expected_unit_count=item["expected_unit_count"],
                is_active=True,
                synced_at=sync_time,
                payload=item.get("payload"),
            )
            session.add(row)
            session.flush()
            counters.created += 1
        else:
            row.site_order_number = item.get("site_order_number")
            row.final_warehouse_id = final_warehouse.id
            row.status = item.get("status") or row.status
            row.expected_unit_count = item["expected_unit_count"]
            row.is_active = True
            row.synced_at = sync_time
            row.payload = item.get("payload")
            counters.updated += 1

        existing_by_key = {unit.unit_key: unit for unit in row.units}
        incoming_keys: set[str] = set()
        for unit_item in item.get("units", []):
            unit_key = unit_item["unit_key"].strip()
            if not unit_key:
                raise _http_error(422, "order plan unit_key is empty")
            incoming_keys.add(unit_key)
            source_warehouse = warehouses.get(unit_item["source_warehouse_external_id"])
            target_warehouse = warehouses.get(unit_item["target_warehouse_external_id"])
            if source_warehouse is None or target_warehouse is None:
                raise _http_error(422, "order plan unit references unknown warehouse")
            unit = existing_by_key.get(unit_key)
            if unit is None:
                unit = LogisticsOrderPlanUnit(plan_id=row.id, unit_key=unit_key)
                session.add(unit)
            unit.source_warehouse_id = source_warehouse.id
            unit.target_warehouse_id = target_warehouse.id
            unit.internal_order_external_id = unit_item.get("internal_order_external_id")
            unit.transfer_external_id = unit_item.get("transfer_external_id")
            unit.is_required = unit_item.get("is_required", True)
            unit.ready_for_handoff = unit_item.get("ready_for_handoff", False)
            unit.readiness = unit_item.get("readiness")
            unit.synced_at = sync_time
            unit.payload = unit_item.get("payload")
            if unit.transfer_external_id:
                transfer = session.scalar(
                    _logistics_unit_selector(SOURCE_TRANSFER, unit.transfer_external_id)
                )
                if transfer is not None:
                    unit.transfer_id = transfer.id
                    transfer.flow_mode = FLOW_ORDER_TRANSFER_V1
                    transfer.plan_key = plan_key
                    transfer.plan_version = plan_version
                    transfer.unit_key = unit_key
                    transfer.expected_unit_count = row.expected_unit_count
                    transfer.ready_for_handoff = unit.ready_for_handoff
                    transfer.is_required = unit.is_required
        for unit in list(row.units):
            if unit.unit_key not in incoming_keys:
                session.delete(unit)
        session.flush()
    session.commit()
    return counters.__dict__


def sync_external_carrier_confirmations(session: Session, items: list[dict]) -> dict:
    """Consume strong handoff facts produced by the existing Bitrix carrier flow."""

    counters = _SyncCounters()
    for item in items:
        plan = _carrier_confirmation_plan(session, item)
        if plan is None:
            raise _http_error(409, "active ORDER_TRANSFER_V1 plan was not found")
        if _has_open_order_flow_conflict(session, plan=plan):
            raise _http_error(409, "order has an unresolved flow conflict")
        if not _plan_is_external_carrier(plan):
            raise _http_error(409, "order plan is not an external carrier flow")
        if plan.final_warehouse.external_id != item["terminal_warehouse_external_id"]:
            _create_order_flow_conflict(
                session,
                origin_order_external_id=plan.origin_order_external_id,
                site_order_number=plan.site_order_number,
                reason="carrier confirmation references another terminal",
                payload={"confirmation": item, "plan_id": plan.id},
            )
            session.commit()
            raise _http_error(409, "carrier terminal differs from the active plan")

        required_units = [unit for unit in plan.units if unit.is_required]
        if not required_units or len(required_units) != plan.expected_unit_count:
            raise _http_error(409, "active plan does not contain all required units")
        if any(unit.transfer is None for unit in required_units):
            raise _http_error(409, "not all required transfer documents were synced")

        changed = False
        for unit in required_units:
            transfer = unit.transfer
            state = _seed_state(session, transfer)
            event_key = (
                f"carrier_confirmation:{item['source_ref']}:{transfer.id}:"
                f"{EVENT_ACCEPTED_AT_POINT}"
            )
            existing = session.scalar(
                select(LogisticsTransferEvent).where(
                    LogisticsTransferEvent.idempotency_key == event_key
                )
            )
            if existing is not None:
                continue
            if not (
                state.status == STATUS_IN_TRANSIT
                and state.dropoff_warehouse_id == plan.final_warehouse_id
            ):
                _create_order_flow_conflict(
                    session,
                    origin_order_external_id=plan.origin_order_external_id,
                    site_order_number=plan.site_order_number,
                    reason="carrier confirmation arrived before the internal leg reached its terminal",
                    payload={
                        "confirmation": item,
                        "plan_id": plan.id,
                        "transfer_id": transfer.id,
                        "state": state.status,
                        "dropoff_warehouse_id": state.dropoff_warehouse_id,
                    },
                )
                session.commit()
                raise _http_error(409, "internal carrier leg is not in transit to its terminal")
            event = LogisticsTransferEvent(
                transfer_id=transfer.id,
                event_type=EVENT_ACCEPTED_AT_POINT,
                event_at=_coerce_datetime(item["confirmed_at"]),
                warehouse_id=plan.final_warehouse_id,
                dropoff_warehouse_id=plan.final_warehouse_id,
                driver_id=state.driver_id,
                user_id=None,
                comment="Confirmed by the existing Bitrix carrier integration",
                source="bitrix_carrier",
                idempotency_key=event_key,
                document_ref=transfer.document_number,
                meta={
                    "carrier_name": item["carrier_name"],
                    "tracking_number": item["tracking_number"],
                    "source_ref": item["source_ref"],
                },
            )
            session.add(event)
            _complete_route_item(
                session,
                transfer_id=transfer.id,
                warehouse_id=plan.final_warehouse_id,
            )
            state.status = STATUS_AT_WAREHOUSE
            state.current_warehouse_id = plan.final_warehouse_id
            state.dropoff_warehouse_id = None
            state.driver_id = None
            state.last_event_type = EVENT_ACCEPTED_AT_POINT
            state.last_event_at = event.event_at
            state.last_user_id = None
            state.last_document_ref = transfer.document_number
            state.version += 1
            changed = True

        payload = dict(plan.payload or {})
        confirmation = {
            "carrier_name": item["carrier_name"],
            "tracking_number": item["tracking_number"],
            "confirmed_at": _coerce_datetime(item["confirmed_at"]).isoformat(),
            "source_ref": item["source_ref"],
        }
        payload["carrier_confirmation"] = confirmation
        plan.payload = payload
        plan.status = "carrier_confirmed"
        if changed:
            counters.created += 1
        else:
            counters.updated += 1
    session.commit()
    return counters.__dict__


def list_external_carrier_confirmations(
    session: Session,
    *,
    confirmed_from: datetime | None = None,
) -> list[dict]:
    plans = (
        session.scalars(
            select(LogisticsOrderPlan)
            .where(
                LogisticsOrderPlan.is_active.is_(True),
                LogisticsOrderPlan.flow_mode == FLOW_ORDER_TRANSFER_V1,
                LogisticsOrderPlan.status == "carrier_confirmed",
            )
            .order_by(LogisticsOrderPlan.id.asc())
        )
        .unique()
        .all()
    )
    results: list[dict] = []
    for plan in plans:
        if not _plan_is_external_carrier(plan):
            continue
        payload = plan.payload if isinstance(plan.payload, dict) else {}
        confirmation = payload.get("carrier_confirmation")
        if not isinstance(confirmation, dict):
            continue
        confirmed_at = _coerce_datetime(confirmation["confirmed_at"])
        if confirmed_from is not None and _same_instant_clock(confirmed_at) < _same_instant_clock(
            confirmed_from
        ):
            continue
        results.append(
            {
                "origin_order_external_id": plan.origin_order_external_id,
                "site_order_number": plan.site_order_number,
                "plan_key": plan.plan_key,
                "plan_version": plan.plan_version,
                "terminal_warehouse_external_id": plan.final_warehouse.external_id,
                **confirmation,
            }
        )
    return results


def _validate_order_transfer_unit(
    session: Session,
    *,
    item: dict,
) -> tuple[LogisticsOrderPlan, LogisticsOrderPlanUnit]:
    required_fields = (
        "origin_order_external_id",
        "plan_key",
        "plan_version",
        "unit_key",
    )
    missing = [field for field in required_fields if not item.get(field)]
    if missing:
        raise _http_error(422, f"ORDER_TRANSFER_V1 unit misses: {', '.join(missing)}")
    plan = (
        session.execute(
            select(LogisticsOrderPlan)
            .where(
                LogisticsOrderPlan.origin_order_external_id == item["origin_order_external_id"],
                LogisticsOrderPlan.plan_key == item["plan_key"],
                LogisticsOrderPlan.plan_version == item["plan_version"],
                LogisticsOrderPlan.is_active.is_(True),
            )
            .options(joinedload(LogisticsOrderPlan.units))
        )
        .unique()
        .scalar_one_or_none()
    )
    if plan is None:
        raise _http_error(409, "active ORDER_TRANSFER_V1 plan was not synced")
    unit = next((row for row in plan.units if row.unit_key == item["unit_key"]), None)
    if unit is None:
        raise _http_error(409, "unit is absent from the active order plan")
    if item.get("expected_unit_count") not in (None, plan.expected_unit_count):
        raise _http_error(409, "unit expected_unit_count differs from the active plan")
    return plan, unit


def sync_units(session: Session, items: list[dict]) -> dict:
    counters = _SyncCounters()
    warehouses = {
        row.external_id: row.id for row in session.scalars(select(LogisticsWarehouse)).all()
    }
    for item in items:
        source_document_type = _normalize_source_document_type(item.get("source_document_type"))
        flow_mode = item.get("flow_mode")
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        if source_document_type == SOURCE_RTU and (
            flow_mode == FLOW_ORDER_TRANSFER_V1 or payload.get("final_sale")
        ):
            # Финальная продажа нового потока не является новой логистической единицей.
            continue
        if flow_mode is None and source_document_type == SOURCE_RTU:
            flow_mode = FLOW_LEGACY_RTU

        plan = None
        plan_unit = None
        if source_document_type == SOURCE_TRANSFER and flow_mode == FLOW_ORDER_TRANSFER_V1:
            plan, plan_unit = _validate_order_transfer_unit(session, item=item)
            legacy_rtu = _legacy_rtu_for_order(
                session,
                origin_order_external_id=item.get("origin_order_external_id"),
                site_order_number=item.get("site_order_number"),
            )
            if legacy_rtu is not None:
                _create_order_flow_conflict(
                    session,
                    origin_order_external_id=item.get("origin_order_external_id"),
                    site_order_number=item.get("site_order_number"),
                    reason="legacy RTU and ORDER_TRANSFER_V1 unit exist for the same order",
                    payload={"unit": item, "legacy_rtu_external_id": legacy_rtu.external_id},
                )
                session.commit()
                raise _http_error(409, "order has both legacy RTU and transfer unit")
        elif source_document_type == SOURCE_RTU:
            disposition, active_plan = order_transfer_rtu_disposition(
                session,
                origin_order_external_id=item.get("origin_order_external_id"),
                site_order_number=item.get("site_order_number"),
            )
            if disposition == "final_sale":
                # Старый SQL-пуллер не знает flow_mode. Полностью завершённый
                # transfer-plan однозначно классифицирует эту РТУ как продажу.
                continue
            if disposition == "conflict" and active_plan is not None:
                _create_order_flow_conflict(
                    session,
                    origin_order_external_id=item.get("origin_order_external_id"),
                    site_order_number=item.get("site_order_number"),
                    reason="legacy RTU was received for an active ORDER_TRANSFER_V1 order",
                    payload={"unit": item, "active_plan_id": active_plan.id},
                )
                session.commit()
                raise _http_error(409, "order has both legacy RTU and transfer plan")
        source_id = warehouses.get(item["source_warehouse_external_id"])
        target_id = warehouses.get(item["target_warehouse_external_id"])
        if source_id is None or target_id is None:
            raise _http_error(422, "transfer references unknown warehouse")
        document_target_id = target_id
        if item.get("document_target_warehouse_external_id"):
            document_target_id = warehouses.get(item["document_target_warehouse_external_id"])
            if document_target_id is None:
                raise _http_error(422, "unit references unknown document target warehouse")
        lookup_code = _lookup_code_for(item)
        barcode = item.get("barcode") or lookup_code
        row = session.scalar(
            _logistics_unit_selector(source_document_type, item["external_id"]).options(
                joinedload(LogisticsTransfer.state)
            )
        )
        created = False
        if row is None:
            row = LogisticsTransfer(
                source_document_type=source_document_type,
                external_id=item["external_id"],
                document_number=item["document_number"],
                document_date=_coerce_datetime(item["document_date"]),
                source_warehouse_id=source_id,
                target_warehouse_id=target_id,
                document_target_warehouse_id=document_target_id,
                final_recipient_name=item.get("final_recipient_name"),
                barcode=barcode,
                lookup_code=lookup_code,
                origin_order_external_id=item.get("origin_order_external_id"),
                site_order_number=item.get("site_order_number"),
                onec_status=item.get("status"),
                flow_mode=flow_mode,
                plan_key=item.get("plan_key"),
                plan_version=item.get("plan_version"),
                unit_key=item.get("unit_key"),
                expected_unit_count=item.get("expected_unit_count"),
                ready_for_handoff=item.get("ready_for_handoff", False),
                is_required=item.get("is_required", True),
                onec_deleted=item.get("onec_deleted", False),
                payload=item.get("payload"),
            )
            session.add(row)
            counters.created += 1
            created = True
        else:
            state = row.state
            active_state = state is not None and not (
                state.last_event_type == EVENT_SYNCED and state.status == STATUS_AT_WAREHOUSE
            )
            has_accounting_conflict = active_state and (
                item.get("onec_deleted", False)
                or row.source_warehouse_id != source_id
                or row.target_warehouse_id != target_id
                or row.document_target_warehouse_id != document_target_id
            )
            if has_accounting_conflict:
                _manual_review_for_sync_conflict(
                    session,
                    row=row,
                    item=item,
                    reason="1C changed or deleted a unit that already has active logistics state",
                )
                session.add(
                    LogisticsTransferEvent(
                        transfer_id=row.id,
                        event_type="onec_reconciliation_conflict",
                        event_at=utcnow(),
                        warehouse_id=state.current_warehouse_id if state is not None else None,
                        dropoff_warehouse_id=(
                            state.dropoff_warehouse_id if state is not None else None
                        ),
                        driver_id=state.driver_id if state is not None else None,
                        user_id=None,
                        comment="1C reconciliation conflict; routed to manual review",
                        source="1c_sync",
                        idempotency_key=None,
                        document_ref=row.document_number,
                        meta={"incoming": item},
                    )
                )
            row.document_number = item["document_number"]
            row.document_date = _coerce_datetime(item["document_date"])
            row.source_warehouse_id = source_id
            row.target_warehouse_id = target_id
            row.document_target_warehouse_id = document_target_id
            row.final_recipient_name = item.get("final_recipient_name")
            row.barcode = barcode
            row.lookup_code = lookup_code
            row.origin_order_external_id = item.get("origin_order_external_id")
            row.site_order_number = item.get("site_order_number")
            row.onec_status = item.get("status")
            row.flow_mode = flow_mode
            row.plan_key = item.get("plan_key")
            row.plan_version = item.get("plan_version")
            row.unit_key = item.get("unit_key")
            row.expected_unit_count = item.get("expected_unit_count")
            row.ready_for_handoff = item.get("ready_for_handoff", False)
            row.is_required = item.get("is_required", True)
            row.onec_deleted = item.get("onec_deleted", False)
            row.payload = item.get("payload")
            counters.updated += 1
        session.flush()
        if plan is not None and plan_unit is not None:
            if plan_unit.transfer_id not in (None, row.id):
                _create_order_flow_conflict(
                    session,
                    origin_order_external_id=row.origin_order_external_id,
                    site_order_number=row.site_order_number,
                    reason="order plan unit is already linked to another transfer",
                    payload={
                        "unit": item,
                        "linked_transfer_id": plan_unit.transfer_id,
                        "incoming_transfer_id": row.id,
                    },
                )
                session.commit()
                raise _http_error(409, "order plan unit is linked to another transfer")
            plan_unit.transfer_id = row.id
            plan_unit.transfer_external_id = row.external_id
            plan_unit.ready_for_handoff = row.ready_for_handoff
            plan_unit.is_required = row.is_required
            plan_unit.readiness = payload.get("readiness") or plan_unit.readiness
            plan_unit.synced_at = utcnow()
        if source_document_type == SOURCE_RTU and not row.site_order_number:
            _create_manual_review(
                session,
                review_type="rtu_without_site_order",
                reason="RTU unit does not contain site_order_number",
                source_document_type=row.source_document_type,
                source_external_id=row.external_id,
                transfer_id=row.id,
                payload={"incoming": item},
            )
        state = session.get(LogisticsTransferState, row.id)
        if state is None:
            state = LogisticsTransferState(
                transfer_id=row.id,
                status=STATUS_AT_WAREHOUSE,
                current_warehouse_id=source_id,
                dropoff_warehouse_id=None,
                driver_id=None,
                last_event_type=EVENT_SYNCED,
                last_event_at=utcnow(),
                last_user_id=None,
                last_document_ref=row.document_number,
                version=1,
            )
            session.add(state)
        elif (
            created is False
            and state.last_event_type == EVENT_SYNCED
            and state.status == STATUS_AT_WAREHOUSE
        ):
            state.current_warehouse_id = source_id
            state.last_document_ref = row.document_number
    session.commit()
    return counters.__dict__


def sync_transfers(session: Session, items: list[dict]) -> dict:
    transfer_items = []
    for item in items:
        item = dict(item)
        item.setdefault("source_document_type", SOURCE_TRANSFER)
        item.setdefault("lookup_code", item.get("barcode"))
        transfer_items.append(item)
    return sync_units(session, transfer_items)


def list_warehouses(session: Session, *, active_only: bool = True) -> list[dict]:
    stmt = select(LogisticsWarehouse).order_by(LogisticsWarehouse.name.asc())
    if active_only:
        stmt = stmt.where(LogisticsWarehouse.is_active.is_(True))
    return [
        {
            "id": row.id,
            "external_id": row.external_id,
            "name": row.name,
            "kind": row.kind,
            "payload": row.payload,
            "is_active": row.is_active,
        }
        for row in session.scalars(stmt).all()
    ]


def list_drivers(session: Session, *, active_only: bool = True) -> list[dict]:
    stmt = select(LogisticsDriver).order_by(LogisticsDriver.full_name.asc())
    if active_only:
        stmt = stmt.where(LogisticsDriver.is_active.is_(True))
    return [
        {
            "id": row.id,
            "external_id": row.external_id,
            "full_name": row.full_name,
            "phone": row.phone,
            "is_active": row.is_active,
        }
        for row in session.scalars(stmt).all()
    ]


def create_route_run(
    session: Session,
    *,
    route_name: str,
    external_id: str | None = None,
    planned_at: datetime | None = None,
    driver_id: int | None = None,
    status: str = "planned",
    payload: dict | None = None,
    items: list[dict] | None = None,
) -> dict:
    _get_driver(session, driver_id)
    route_run = None
    if external_id:
        route_run = session.scalar(
            select(LogisticsRouteRun).where(LogisticsRouteRun.external_id == external_id)
        )
    if route_run is None:
        route_run = LogisticsRouteRun(
            external_id=external_id,
            route_name=route_name,
            planned_at=planned_at,
            driver_id=driver_id,
            status=status,
            payload=payload,
        )
        session.add(route_run)
        session.flush()
    else:
        route_run.route_name = route_name
        route_run.planned_at = planned_at
        route_run.driver_id = driver_id
        route_run.status = status
        route_run.payload = payload
    for index, item in enumerate(items or [], start=1):
        transfer = None
        if item.get("transfer_id"):
            transfer = session.get(LogisticsTransfer, item["transfer_id"])
        elif item.get("lookup_code"):
            transfer = _get_unit_by_lookup(session, item["lookup_code"])
        if transfer is None:
            raise _http_error(404, "route item transfer not found")
        dropoff_warehouse_id = item.get("dropoff_warehouse_id")
        if dropoff_warehouse_id is not None:
            _get_warehouse(session, dropoff_warehouse_id)
        route_item = _get_or_create_route_item(
            session,
            route_run_id=route_run.id,
            transfer_id=transfer.id,
            dropoff_warehouse_id=dropoff_warehouse_id,
        )
        if route_item is not None:
            route_item.leg_sequence = item.get("leg_sequence") or index
            route_item.status = item.get("status") or route_item.status
    session.commit()
    session.refresh(route_run)
    return _serialize_route_run(route_run)


def _serialize_route_run(route_run: LogisticsRouteRun) -> dict:
    items = sorted(
        route_run.items,
        key=lambda item: (item.leg_sequence is None, item.leg_sequence or 0, item.id),
    )
    return {
        "id": route_run.id,
        "external_id": route_run.external_id,
        "route_name": route_run.route_name,
        "planned_at": route_run.planned_at,
        "driver_id": route_run.driver_id,
        "driver_name": route_run.driver.full_name if route_run.driver is not None else None,
        "status": route_run.status,
        "payload": route_run.payload,
        "items": [
            {
                "id": item.id,
                "transfer_id": item.transfer_id,
                "source_document_type": item.transfer.source_document_type,
                "external_id": item.transfer.external_id,
                "document_number": item.transfer.document_number,
                "barcode": item.transfer.barcode,
                "lookup_code": item.transfer.lookup_code,
                "dropoff_warehouse_id": item.dropoff_warehouse_id,
                "dropoff_warehouse_name": (
                    item.dropoff_warehouse.name if item.dropoff_warehouse is not None else None
                ),
                "leg_sequence": item.leg_sequence,
                "status": item.status,
                "completed_at": item.completed_at,
            }
            for item in items
        ],
    }


def list_route_runs(
    session: Session,
    *,
    status: str | None = None,
    driver_id: int | None = None,
) -> list[dict]:
    stmt = (
        select(LogisticsRouteRun)
        .options(
            joinedload(LogisticsRouteRun.driver),
            joinedload(LogisticsRouteRun.items).joinedload(LogisticsRouteRunItem.transfer),
            joinedload(LogisticsRouteRun.items).joinedload(LogisticsRouteRunItem.dropoff_warehouse),
        )
        .order_by(LogisticsRouteRun.planned_at.desc().nullslast(), LogisticsRouteRun.id.desc())
    )
    if status is not None:
        stmt = stmt.where(LogisticsRouteRun.status == status)
    if driver_id is not None:
        stmt = stmt.where(LogisticsRouteRun.driver_id == driver_id)
    return [_serialize_route_run(row) for row in session.scalars(stmt).unique().all()]


def list_manual_reviews(
    session: Session,
    *,
    status: str | None = "open",
    review_type: str | None = None,
) -> list[dict]:
    stmt = (
        select(LogisticsManualReview)
        .options(
            joinedload(LogisticsManualReview.transfer),
            joinedload(LogisticsManualReview.resolved_by_user),
        )
        .order_by(LogisticsManualReview.created_at.desc(), LogisticsManualReview.id.desc())
    )
    if status is not None:
        stmt = stmt.where(LogisticsManualReview.status == status)
    if review_type is not None:
        stmt = stmt.where(LogisticsManualReview.review_type == review_type)
    rows = session.scalars(stmt).all()
    return [
        {
            "id": row.id,
            "review_type": row.review_type,
            "status": row.status,
            "source_document_type": row.source_document_type,
            "source_external_id": row.source_external_id,
            "transfer_id": row.transfer_id,
            "document_number": row.transfer.document_number if row.transfer is not None else None,
            "reason": row.reason,
            "payload": row.payload,
            "resolved_by_user_id": row.resolved_by_user_id,
            "resolved_by_user_name": (
                row.resolved_by_user.full_name if row.resolved_by_user is not None else None
            ),
            "resolved_at": row.resolved_at,
            "created_at": row.created_at,
        }
        for row in rows
    ]


def handoff_to_external_carrier(
    session: Session,
    *,
    transfer_id: int,
    actor_user_id: int,
    carrier_name: str,
    tracking_number: str | None = None,
    carrier_terminal: str | None = None,
    comment: str | None = None,
    idempotency_key: str | None = None,
) -> dict:
    actor = _get_actor(session, actor_user_id)
    _require_role(actor, ROLE_LOGIST)
    transfer = session.get(LogisticsTransfer, transfer_id)
    if transfer is None:
        raise _http_error(404, "transfer not found")
    state = _seed_state(session, transfer)
    if state.status != STATUS_IN_TRANSIT:
        raise _http_error(409, "transfer must be in transit before external carrier handoff")
    event_key = (
        f"{idempotency_key}:{transfer_id}:{EVENT_HANDED_TO_EXTERNAL_CARRIER}"
        if idempotency_key
        else None
    )
    if event_key is not None:
        existing = session.scalar(
            select(LogisticsTransferEvent).where(
                LogisticsTransferEvent.idempotency_key == event_key
            )
        )
        if existing is not None:
            return {"status": "ok"}
    event = LogisticsTransferEvent(
        transfer_id=transfer_id,
        event_type=EVENT_HANDED_TO_EXTERNAL_CARRIER,
        event_at=utcnow(),
        warehouse_id=None,
        dropoff_warehouse_id=state.dropoff_warehouse_id,
        driver_id=state.driver_id,
        user_id=actor.id,
        comment=comment,
        source="api",
        idempotency_key=event_key,
        document_ref=transfer.document_number,
        meta={
            "carrier_name": carrier_name,
            "tracking_number": tracking_number,
            "carrier_terminal": carrier_terminal,
        },
    )
    session.add(event)
    state.status = STATUS_WITH_EXTERNAL_CARRIER
    state.current_warehouse_id = None
    state.driver_id = None
    state.last_event_type = EVENT_HANDED_TO_EXTERNAL_CARRIER
    state.last_event_at = event.event_at
    state.last_user_id = actor.id
    state.last_document_ref = transfer.document_number
    state.version += 1
    session.commit()
    return {"status": "ok"}


def handoff_to_external_carrier_from_sync(
    session: Session,
    *,
    transfer_id: int,
    carrier_name: str,
    tracking_number: str | None = None,
    carrier_terminal: str | None = None,
    comment: str | None = None,
    idempotency_key: str | None = None,
    meta: dict | None = None,
) -> dict:
    transfer = session.get(LogisticsTransfer, transfer_id)
    if transfer is None:
        raise _http_error(404, "transfer not found")
    state = _seed_state(session, transfer)
    event_key = (
        f"{idempotency_key}:{transfer_id}:{EVENT_HANDED_TO_EXTERNAL_CARRIER}"
        if idempotency_key
        else None
    )
    if event_key is not None:
        existing = session.scalar(
            select(LogisticsTransferEvent).where(
                LogisticsTransferEvent.idempotency_key == event_key
            )
        )
        if existing is not None:
            return {"status": "existing"}
    if state.status == STATUS_WITH_EXTERNAL_CARRIER:
        return {"status": "existing"}
    if state.status != STATUS_AT_WAREHOUSE or state.last_event_type not in {
        EVENT_SYNCED,
        EVENT_MANUAL_READY_OVERRIDE,
    }:
        return {
            "status": "conflict",
            "detail": "transfer already has active logistics state",
        }

    previous_state = {
        "status": state.status,
        "current_warehouse_id": state.current_warehouse_id,
        "dropoff_warehouse_id": state.dropoff_warehouse_id,
        "driver_id": state.driver_id,
        "last_event_type": state.last_event_type,
    }
    event = LogisticsTransferEvent(
        transfer_id=transfer_id,
        event_type=EVENT_HANDED_TO_EXTERNAL_CARRIER,
        event_at=utcnow(),
        warehouse_id=state.current_warehouse_id,
        dropoff_warehouse_id=state.dropoff_warehouse_id,
        driver_id=state.driver_id,
        user_id=None,
        comment=comment,
        source="1c_sync",
        idempotency_key=event_key,
        document_ref=transfer.document_number,
        meta={
            "carrier_name": carrier_name,
            "tracking_number": tracking_number,
            "carrier_terminal": carrier_terminal,
            "previous_state": previous_state,
            **(meta or {}),
        },
    )
    session.add(event)
    state.status = STATUS_WITH_EXTERNAL_CARRIER
    state.current_warehouse_id = None
    state.dropoff_warehouse_id = None
    state.driver_id = None
    state.last_event_type = EVENT_HANDED_TO_EXTERNAL_CARRIER
    state.last_event_at = event.event_at
    state.last_user_id = None
    state.last_document_ref = transfer.document_number
    state.version += 1
    session.commit()
    return {"status": "created"}


def accept_from_external_carrier(
    session: Session,
    *,
    transfer_id: int,
    actor_user_id: int,
    warehouse_id: int,
    comment: str | None = None,
    idempotency_key: str | None = None,
) -> dict:
    actor = _get_actor(session, actor_user_id)
    _require_role(actor, ROLE_RECEIVER)
    _get_warehouse(session, warehouse_id)
    transfer = session.get(LogisticsTransfer, transfer_id)
    if transfer is None:
        raise _http_error(404, "transfer not found")
    state = _seed_state(session, transfer)
    if state.status != STATUS_WITH_EXTERNAL_CARRIER:
        raise _http_error(409, "transfer is not with external carrier")
    expected_warehouse_id = transfer.document_target_warehouse_id or transfer.target_warehouse_id
    if warehouse_id != expected_warehouse_id and actor.role not in ROLE_LOGIST:
        raise _http_error(409, "external carrier acceptance warehouse does not match target")
    event_key = (
        f"{idempotency_key}:{transfer_id}:{EVENT_ACCEPTED_FROM_EXTERNAL_CARRIER}"
        if idempotency_key
        else None
    )
    if event_key is not None:
        existing = session.scalar(
            select(LogisticsTransferEvent).where(
                LogisticsTransferEvent.idempotency_key == event_key
            )
        )
        if existing is not None:
            return {"status": "ok"}
    event = LogisticsTransferEvent(
        transfer_id=transfer_id,
        event_type=EVENT_ACCEPTED_FROM_EXTERNAL_CARRIER,
        event_at=utcnow(),
        warehouse_id=warehouse_id,
        dropoff_warehouse_id=state.dropoff_warehouse_id,
        driver_id=None,
        user_id=actor.id,
        comment=comment,
        source="api",
        idempotency_key=event_key,
        document_ref=transfer.document_number,
        meta={"accepted_from_external_carrier": True},
    )
    session.add(event)
    session.flush()
    _complete_route_item(session, transfer_id=transfer_id, warehouse_id=warehouse_id)
    _bridge_rtu_receipt_to_order_fulfillment(
        session,
        transfer=transfer,
        event=event,
        warehouse_id=warehouse_id,
    )
    state.status = STATUS_AT_WAREHOUSE
    state.current_warehouse_id = warehouse_id
    state.dropoff_warehouse_id = None
    state.driver_id = None
    state.last_event_type = EVENT_ACCEPTED_FROM_EXTERNAL_CARRIER
    state.last_event_at = event.event_at
    state.last_user_id = actor.id
    state.last_document_ref = transfer.document_number
    state.version += 1
    session.commit()
    return {"status": "ok"}


def manual_ready_override(
    session: Session,
    *,
    actor_user_id: int,
    source_document_type: str,
    external_id: str,
    warehouse_id: int,
    reason: str,
    lookup_code: str | None = None,
    site_order_number: str | None = None,
) -> dict:
    actor = _get_actor(session, actor_user_id)
    _require_role(actor, ROLE_LOGIST)
    source_document_type = _normalize_source_document_type(source_document_type)
    _get_warehouse(session, warehouse_id)
    transfer = session.scalar(_logistics_unit_selector(source_document_type, external_id))
    if transfer is None:
        raise _http_error(404, "transfer not found")
    if lookup_code:
        transfer.lookup_code = lookup_code
    if site_order_number:
        transfer.site_order_number = site_order_number
    state = _seed_state(session, transfer)
    event = LogisticsTransferEvent(
        transfer_id=transfer.id,
        event_type=EVENT_MANUAL_READY_OVERRIDE,
        event_at=utcnow(),
        warehouse_id=warehouse_id,
        dropoff_warehouse_id=None,
        driver_id=None,
        user_id=actor.id,
        comment=reason,
        source="api",
        idempotency_key=None,
        document_ref=transfer.document_number,
        meta={
            "reason": reason,
            "source_document_type": source_document_type,
            "external_id": external_id,
        },
    )
    session.add(event)
    _create_manual_review(
        session,
        review_type="manual_ready_override",
        reason=reason,
        source_document_type=source_document_type,
        source_external_id=external_id,
        transfer_id=transfer.id,
        payload={"warehouse_id": warehouse_id},
    )
    state.status = STATUS_AT_WAREHOUSE
    state.current_warehouse_id = warehouse_id
    state.dropoff_warehouse_id = None
    state.driver_id = None
    state.last_event_type = EVENT_MANUAL_READY_OVERRIDE
    state.last_event_at = event.event_at
    state.last_user_id = actor.id
    state.last_document_ref = transfer.document_number
    state.version += 1
    session.commit()
    return {"status": "ok", "transfer_id": transfer.id}
