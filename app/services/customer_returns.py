from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.models.customer_return import (
    CustomerReturnAction,
    CustomerReturnEvent,
    CustomerReturnShipment,
)
from app.services.customer_return_carriers import (
    STATUS_ARRIVED,
    STATUS_CANCELLED,
    STATUS_EXCEPTION,
    STATUS_IN_TRANSIT,
    STATUS_REGISTERED,
    get_customer_return_carrier_adapter,
)

STATUS_PICKED_UP = "picked_up"
STATUS_ONEC_RETURN_CONFIRMED = "onec_return_confirmed"

EVENT_REGISTERED = "registered"
EVENT_CARRIER_STATUS = "carrier_status"
EVENT_PICKUP_CONFIRMED = "pickup_confirmed"
EVENT_ONEC_RETURN_CONFIRMED = "onec_return_confirmed"

ACTION_ARRIVAL_TASK = "arrival_task"
ACTION_STORAGE_REMINDER_3D = "storage_reminder_3d"
ACTION_STORAGE_REMINDER_1D = "storage_reminder_1d"
ACTION_ONEC_RETURN_CONTROL = "onec_return_control"
ACTION_COMPLETE_RETURN_TASK = "complete_return_task"

ACTION_PENDING = "pending"
ACTION_COMPLETED = "completed"
ACTION_SKIPPED = "skipped"

_BUSINESS_TERMINAL_STATUSES = {STATUS_PICKED_UP, STATUS_ONEC_RETURN_CONFIRMED}
_TRANSPORT_STATUS_RANK = {
    STATUS_REGISTERED: 0,
    STATUS_IN_TRANSIT: 1,
    STATUS_ARRIVED: 2,
    STATUS_CANCELLED: 3,
}


class CustomerReturnError(RuntimeError):
    pass


class CustomerReturnNotFound(CustomerReturnError):
    pass


class CustomerReturnConflict(CustomerReturnError):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _dedupe_key(*parts: object) -> str:
    value = "|".join(str(part) for part in parts)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _shipment_query():
    return select(CustomerReturnShipment).options(
        selectinload(CustomerReturnShipment.events),
        selectinload(CustomerReturnShipment.actions),
    )


def get_return(db: Session, shipment_id: int) -> CustomerReturnShipment:
    shipment = db.scalar(_shipment_query().where(CustomerReturnShipment.id == shipment_id))
    if shipment is None:
        raise CustomerReturnNotFound("customer return shipment not found")
    return shipment


def list_returns(
    db: Session,
    *,
    carrier: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[CustomerReturnShipment]:
    statement = select(CustomerReturnShipment)
    if carrier:
        statement = statement.where(CustomerReturnShipment.carrier == carrier)
    if status:
        statement = statement.where(CustomerReturnShipment.status == status)
    statement = statement.order_by(
        CustomerReturnShipment.updated_at.desc(), CustomerReturnShipment.id.desc()
    ).limit(limit)
    return list(db.scalars(statement).all())


def _find_return_by_tracking(
    db: Session,
    *,
    carrier: str,
    tracking_number: str,
) -> CustomerReturnShipment | None:
    return db.scalar(
        select(CustomerReturnShipment).where(
            CustomerReturnShipment.carrier == carrier,
            CustomerReturnShipment.tracking_number == tracking_number,
        )
    )


def _fill_missing_registration_links(
    db: Session,
    shipment: CustomerReturnShipment,
    *,
    source_ref: str | None,
    bitrix_case_id: str | None,
    site_ticket_id: str | None,
    onec_order_ref: str | None,
    created_by_bitrix_user_id: str | None,
    payload: dict | None,
) -> CustomerReturnShipment:
    changed = False
    for field, value in (
        ("source_ref", source_ref),
        ("bitrix_case_id", bitrix_case_id),
        ("site_ticket_id", site_ticket_id),
        ("onec_order_ref", onec_order_ref),
        ("created_by_bitrix_user_id", created_by_bitrix_user_id),
        ("source_payload", payload),
    ):
        if value is not None and getattr(shipment, field) is None:
            setattr(shipment, field, value)
            changed = True
    if changed:
        shipment.updated_at = _utcnow()
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise CustomerReturnConflict(
                "customer return registration links conflict with existing data"
            ) from exc
    return get_return(db, shipment.id)


def register_return(
    db: Session,
    *,
    carrier: str,
    tracking_number: str,
    source: str,
    source_ref: str | None = None,
    bitrix_case_id: str | None = None,
    site_ticket_id: str | None = None,
    onec_order_ref: str | None = None,
    created_by_bitrix_user_id: str | None = None,
    payload: dict | None = None,
) -> tuple[CustomerReturnShipment, bool]:
    adapter = get_customer_return_carrier_adapter(carrier)
    normalized_tracking = adapter.normalize_tracking_number(tracking_number)

    source_match = None
    if source_ref:
        source_match = db.scalar(
            select(CustomerReturnShipment).where(CustomerReturnShipment.source_ref == source_ref)
        )

    existing = _find_return_by_tracking(
        db,
        carrier=adapter.carrier,
        tracking_number=normalized_tracking,
    )
    if existing is not None:
        if source_match is not None and source_match.id != existing.id:
            raise CustomerReturnConflict(
                "source_ref already belongs to another customer return shipment"
            )
        return (
            _fill_missing_registration_links(
                db,
                existing,
                source_ref=source_ref,
                bitrix_case_id=bitrix_case_id,
                site_ticket_id=site_ticket_id,
                onec_order_ref=onec_order_ref,
                created_by_bitrix_user_id=created_by_bitrix_user_id,
                payload=payload,
            ),
            False,
        )

    if source_match is not None:
        raise CustomerReturnConflict(
            "source_ref already belongs to another customer return shipment"
        )

    now = _utcnow()
    shipment = CustomerReturnShipment(
        carrier=adapter.carrier,
        tracking_number=normalized_tracking,
        status=STATUS_REGISTERED,
        status_changed_at=now,
        source=source,
        source_ref=source_ref,
        bitrix_case_id=bitrix_case_id,
        site_ticket_id=site_ticket_id,
        onec_order_ref=onec_order_ref,
        created_by_bitrix_user_id=created_by_bitrix_user_id,
        source_payload=payload,
        updated_at=now,
    )
    try:
        db.add(shipment)
        db.flush()
        db.add(
            CustomerReturnEvent(
                shipment_id=shipment.id,
                event_type=EVENT_REGISTERED,
                source=source,
                normalized_status=STATUS_REGISTERED,
                dedupe_key=_dedupe_key("registered", adapter.carrier, normalized_tracking),
                actor_bitrix_user_id=created_by_bitrix_user_id,
                occurred_at=now,
                payload=payload,
            )
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        existing = _find_return_by_tracking(
            db,
            carrier=adapter.carrier,
            tracking_number=normalized_tracking,
        )
        if existing is not None:
            return (
                _fill_missing_registration_links(
                    db,
                    existing,
                    source_ref=source_ref,
                    bitrix_case_id=bitrix_case_id,
                    site_ticket_id=site_ticket_id,
                    onec_order_ref=onec_order_ref,
                    created_by_bitrix_user_id=created_by_bitrix_user_id,
                    payload=payload,
                ),
                False,
            )
        raise CustomerReturnConflict(
            "customer return registration conflicts with existing data"
        ) from exc
    return get_return(db, shipment.id), True


def _carrier_event_dedupe_key(
    shipment: CustomerReturnShipment,
    *,
    status_code: str,
    occurred_at: datetime,
    external_event_id: str | None,
    idempotency_key: str | None,
) -> str:
    if idempotency_key:
        return _dedupe_key("carrier-event", shipment.id, "request", idempotency_key)
    if external_event_id:
        return _dedupe_key("carrier-event", shipment.id, "external", external_event_id)
    return _dedupe_key(
        "carrier-event",
        shipment.carrier,
        shipment.tracking_number,
        status_code.strip().upper(),
        _as_utc(occurred_at).isoformat(),
    )


def _should_apply_transport_status(current: str, candidate: str) -> bool:
    if current in _BUSINESS_TERMINAL_STATUSES:
        return False
    if candidate == STATUS_EXCEPTION:
        return True
    if current == STATUS_EXCEPTION:
        return True
    current_rank = _TRANSPORT_STATUS_RANK.get(current, -1)
    candidate_rank = _TRANSPORT_STATUS_RANK.get(candidate, -1)
    return candidate_rank >= current_rank


def _is_current_carrier_event(
    shipment: CustomerReturnShipment,
    occurred_at: datetime,
) -> bool:
    if shipment.carrier_last_event_at is None:
        return True
    return occurred_at >= _as_utc(shipment.carrier_last_event_at)


def _schedule_action(
    db: Session,
    shipment: CustomerReturnShipment,
    *,
    action_type: str,
    due_at: datetime,
    payload: dict | None = None,
) -> CustomerReturnAction:
    dedupe_key = _dedupe_key("return-action", shipment.id, action_type)
    action = db.scalar(
        select(CustomerReturnAction).where(CustomerReturnAction.dedupe_key == dedupe_key)
    )
    now = _utcnow()
    if action is not None:
        if action.status == ACTION_PENDING:
            action.due_at = _as_utc(due_at)
            action.payload = payload
            action.updated_at = now
        return action
    action = CustomerReturnAction(
        shipment_id=shipment.id,
        action_type=action_type,
        status=ACTION_PENDING,
        due_at=_as_utc(due_at),
        dedupe_key=dedupe_key,
        payload=payload,
        updated_at=now,
    )
    db.add(action)
    return action


def _schedule_arrival_actions(
    db: Session,
    shipment: CustomerReturnShipment,
    *,
    now: datetime,
) -> None:
    common_payload = {
        "carrier": shipment.carrier,
        "tracking_number": shipment.tracking_number,
        "bitrix_case_id": shipment.bitrix_case_id,
    }
    _schedule_action(
        db,
        shipment,
        action_type=ACTION_ARRIVAL_TASK,
        due_at=now,
        payload=common_payload,
    )
    if shipment.storage_deadline_at is None:
        return
    deadline = _as_utc(shipment.storage_deadline_at)
    for action_type, days_before in (
        (ACTION_STORAGE_REMINDER_3D, 3),
        (ACTION_STORAGE_REMINDER_1D, 1),
    ):
        due_at = deadline - timedelta(days=days_before)
        if due_at <= now:
            continue
        _schedule_action(
            db,
            shipment,
            action_type=action_type,
            due_at=due_at,
            payload={**common_payload, "storage_deadline_at": deadline.isoformat()},
        )


def record_carrier_event(
    db: Session,
    shipment_id: int,
    *,
    status_code: str,
    occurred_at: datetime,
    status_text: str | None = None,
    external_event_id: str | None = None,
    idempotency_key: str | None = None,
    storage_deadline_at: datetime | None = None,
    payload: dict | None = None,
) -> tuple[CustomerReturnShipment, bool]:
    shipment = get_return(db, shipment_id)
    occurred_at = _as_utc(occurred_at)
    dedupe_key = _carrier_event_dedupe_key(
        shipment,
        status_code=status_code,
        occurred_at=occurred_at,
        external_event_id=external_event_id,
        idempotency_key=idempotency_key,
    )
    existing = db.scalar(
        select(CustomerReturnEvent).where(CustomerReturnEvent.dedupe_key == dedupe_key)
    )
    if existing is not None:
        return shipment, False

    adapter = get_customer_return_carrier_adapter(shipment.carrier)
    normalized = adapter.normalize_status(status_code)
    raw_status_code = status_code.strip()
    event = CustomerReturnEvent(
        shipment_id=shipment.id,
        event_type=EVENT_CARRIER_STATUS,
        source=shipment.carrier,
        normalized_status=normalized.status,
        carrier_status_code=raw_status_code,
        carrier_status_text=status_text,
        external_event_id=external_event_id,
        dedupe_key=dedupe_key,
        occurred_at=occurred_at,
        payload=payload,
    )
    db.add(event)

    is_current_event = _is_current_carrier_event(shipment, occurred_at)
    if is_current_event:
        shipment.carrier_last_status_code = raw_status_code
        shipment.carrier_last_status_text = status_text
        shipment.carrier_last_event_at = occurred_at
        if storage_deadline_at is not None:
            shipment.storage_deadline_at = _as_utc(storage_deadline_at)

    status_applied = is_current_event and _should_apply_transport_status(
        shipment.status,
        normalized.status,
    )
    if status_applied:
        shipment.status = normalized.status
        shipment.status_changed_at = occurred_at
        if normalized.status == STATUS_ARRIVED and shipment.arrived_at is None:
            shipment.arrived_at = occurred_at
    shipment.updated_at = _utcnow()

    if status_applied and normalized.status == STATUS_ARRIVED:
        _schedule_arrival_actions(db, shipment, now=shipment.updated_at)
    elif is_current_event and shipment.status == STATUS_ARRIVED and storage_deadline_at is not None:
        _schedule_arrival_actions(db, shipment, now=shipment.updated_at)

    db.commit()
    return get_return(db, shipment.id), True


def confirm_pickup(
    db: Session,
    shipment_id: int,
    *,
    actor_bitrix_user_id: str,
    occurred_at: datetime,
    idempotency_key: str | None = None,
    comment: str | None = None,
) -> CustomerReturnShipment:
    shipment = get_return(db, shipment_id)
    existing = db.scalar(
        select(CustomerReturnEvent).where(
            CustomerReturnEvent.shipment_id == shipment.id,
            CustomerReturnEvent.event_type == EVENT_PICKUP_CONFIRMED,
        )
    )
    if existing is not None:
        return shipment

    occurred_at = _as_utc(occurred_at)
    db.add(
        CustomerReturnEvent(
            shipment_id=shipment.id,
            event_type=EVENT_PICKUP_CONFIRMED,
            source="bitrix24",
            normalized_status=STATUS_PICKED_UP,
            dedupe_key=_dedupe_key(
                "pickup",
                shipment.id,
                idempotency_key or "confirmed",
            ),
            actor_bitrix_user_id=actor_bitrix_user_id,
            occurred_at=occurred_at,
            payload={"comment": comment} if comment else None,
        )
    )
    if shipment.status != STATUS_ONEC_RETURN_CONFIRMED:
        shipment.status = STATUS_PICKED_UP
        shipment.status_changed_at = occurred_at
    shipment.picked_up_at = occurred_at
    shipment.picked_up_by_bitrix_user_id = actor_bitrix_user_id
    shipment.updated_at = _utcnow()
    _schedule_action(
        db,
        shipment,
        action_type=ACTION_ONEC_RETURN_CONTROL,
        due_at=shipment.updated_at,
        payload={
            "carrier": shipment.carrier,
            "tracking_number": shipment.tracking_number,
            "onec_order_ref": shipment.onec_order_ref,
        },
    )
    db.commit()
    return get_return(db, shipment.id)


def confirm_onec_return(
    db: Session,
    shipment_id: int,
    *,
    onec_return_ref: str,
    occurred_at: datetime,
    idempotency_key: str | None = None,
) -> CustomerReturnShipment:
    shipment = get_return(db, shipment_id)
    existing = db.scalar(
        select(CustomerReturnEvent).where(
            CustomerReturnEvent.shipment_id == shipment.id,
            CustomerReturnEvent.event_type == EVENT_ONEC_RETURN_CONFIRMED,
        )
    )
    if existing is not None:
        if shipment.onec_return_ref != onec_return_ref:
            raise CustomerReturnConflict(
                "customer return is already linked to another 1C return document"
            )
        return shipment

    occurred_at = _as_utc(occurred_at)
    db.add(
        CustomerReturnEvent(
            shipment_id=shipment.id,
            event_type=EVENT_ONEC_RETURN_CONFIRMED,
            source="onec_readonly",
            normalized_status=STATUS_ONEC_RETURN_CONFIRMED,
            dedupe_key=_dedupe_key(
                "onec-return",
                shipment.id,
                idempotency_key or onec_return_ref,
            ),
            occurred_at=occurred_at,
            payload={"onec_return_ref": onec_return_ref},
        )
    )
    shipment.status = STATUS_ONEC_RETURN_CONFIRMED
    shipment.status_changed_at = occurred_at
    shipment.onec_return_ref = onec_return_ref
    shipment.onec_return_confirmed_at = occurred_at
    shipment.updated_at = _utcnow()
    control_action = db.scalar(
        select(CustomerReturnAction).where(
            CustomerReturnAction.shipment_id == shipment.id,
            CustomerReturnAction.action_type == ACTION_ONEC_RETURN_CONTROL,
        )
    )
    if control_action is not None and control_action.status == ACTION_PENDING:
        control_action.status = ACTION_SKIPPED
        control_action.completed_at = occurred_at
        control_action.updated_at = shipment.updated_at
    arrival_action = db.scalar(
        select(CustomerReturnAction).where(
            CustomerReturnAction.shipment_id == shipment.id,
            CustomerReturnAction.action_type == ACTION_ARRIVAL_TASK,
        )
    )
    if arrival_action is not None:
        _schedule_action(
            db,
            shipment,
            action_type=ACTION_COMPLETE_RETURN_TASK,
            due_at=shipment.updated_at,
            payload={
                "carrier": shipment.carrier,
                "tracking_number": shipment.tracking_number,
                "onec_return_ref": onec_return_ref,
            },
        )
    db.commit()
    return get_return(db, shipment.id)


def list_due_actions(
    db: Session,
    *,
    as_of: datetime,
    limit: int = 100,
) -> list[CustomerReturnAction]:
    statement = (
        select(CustomerReturnAction)
        .where(
            CustomerReturnAction.status == ACTION_PENDING,
            CustomerReturnAction.due_at <= _as_utc(as_of),
            (
                CustomerReturnAction.next_attempt_at.is_(None)
                | (CustomerReturnAction.next_attempt_at <= _as_utc(as_of))
            ),
        )
        .order_by(CustomerReturnAction.due_at, CustomerReturnAction.id)
        .limit(limit)
    )
    return list(db.scalars(statement).all())


def complete_action(
    db: Session,
    action_id: int,
    *,
    external_reference: str,
    completed_at: datetime,
) -> CustomerReturnAction:
    action = db.get(CustomerReturnAction, action_id)
    if action is None:
        raise CustomerReturnNotFound("customer return action not found")
    if action.status == ACTION_COMPLETED:
        if action.external_reference != external_reference:
            raise CustomerReturnConflict(
                "customer return action is already completed with another reference"
            )
        return action
    if action.status == ACTION_SKIPPED:
        raise CustomerReturnConflict("customer return action is already skipped")
    now = _utcnow()
    action.status = ACTION_COMPLETED
    action.external_reference = external_reference
    action.completed_at = _as_utc(completed_at)
    action.attempt_count += 1
    action.last_error = None
    action.next_attempt_at = None
    action.lease_token = None
    action.leased_until = None
    action.updated_at = now
    db.commit()
    db.refresh(action)
    return action
