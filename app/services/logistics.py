from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import Select, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.models import (
    LogisticsDraft,
    LogisticsDraftItem,
    LogisticsDriver,
    LogisticsEventPhoto,
    LogisticsTransfer,
    LogisticsTransferEvent,
    LogisticsTransferState,
    LogisticsUser,
    LogisticsWarehouse,
)

ROLE_SENDER = {"sender", "logist", "admin"}
ROLE_RECEIVER = {"receiver", "logist", "admin"}
ROLE_LOGIST = {"logist", "admin"}

STATUS_AT_WAREHOUSE = "at_warehouse"
STATUS_IN_TRANSIT = "in_transit"

EVENT_SYNCED = "synced"
EVENT_HANDED_TO_DRIVER = "handed_to_driver"
EVENT_ACCEPTED_AT_POINT = "accepted_at_point"
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


def _get_transfer_by_barcode(session: Session, barcode: str) -> LogisticsTransfer:
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


def add_scan_to_draft(
    session: Session,
    *,
    draft_id: int,
    actor_user_id: int,
    barcode: str,
    dropoff_warehouse_id: int | None = None,
) -> dict:
    draft = _get_draft(session, draft_id)
    if draft.status != "open":
        raise _http_error(409, "draft is already closed")
    actor = _get_actor(session, actor_user_id)
    if actor.id != draft.actor_user_id and actor.role not in ROLE_LOGIST:
        raise _http_error(403, "user cannot modify this draft")

    transfer = _get_transfer_by_barcode(session, barcode)
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
    else:
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
        barcode=barcode,
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
) -> dict:
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
                source="telegram",
                idempotency_key=event_key,
                document_ref=transfer.document_number,
                meta={"draft_id": draft.id},
            )
            _attach_photos(event, photos)
            session.add(event)
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
                source="telegram",
                idempotency_key=event_key,
                document_ref=transfer.document_number,
                meta={"draft_id": draft.id},
            )
            _attach_photos(event, photos)
            session.add(event)
            state.status = STATUS_AT_WAREHOUSE
            state.current_warehouse_id = draft.warehouse_id
            state.dropoff_warehouse_id = None
            state.driver_id = None
            state.last_event_type = EVENT_ACCEPTED_AT_POINT

        state.last_event_at = event.event_at
        state.last_user_id = actor.id
        state.last_document_ref = transfer.document_number
        state.version += 1
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
    warehouse_id: int,
    driver_id: int | None = None,
) -> list[dict]:
    stmt: Select[tuple[LogisticsTransferState]] = (
        select(LogisticsTransferState)
        .where(
            LogisticsTransferState.status == STATUS_IN_TRANSIT,
            LogisticsTransferState.dropoff_warehouse_id == warehouse_id,
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
                "document_number": transfer.document_number,
                "barcode": transfer.barcode,
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


def list_monitor(
    session: Session,
    *,
    status: str | None = None,
    warehouse_id: int | None = None,
    driver_id: int | None = None,
    final_recipient: str | None = None,
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
        payload.append(
            {
                "transfer_id": transfer.id,
                "external_id": transfer.external_id,
                "document_number": transfer.document_number,
                "document_date": transfer.document_date,
                "barcode": transfer.barcode,
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
                is_active=item.get("is_active", True),
            )
            session.add(row)
            counters.created += 1
        else:
            row.name = item["name"]
            row.kind = item.get("kind", row.kind)
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
        warehouse_id = None
        if item.get("default_warehouse_external_id") is not None:
            warehouse_id = warehouses.get(item["default_warehouse_external_id"])
            if warehouse_id is None:
                raise _http_error(422, "user references unknown default warehouse")
        if row is None:
            row = LogisticsUser(
                external_id=item.get("external_id"),
                telegram_user_id=item.get("telegram_user_id"),
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
            row.username = item.get("username", row.username)
            row.full_name = item["full_name"]
            row.role = item["role"]
            row.default_warehouse_id = warehouse_id
            row.is_active = item.get("is_active", row.is_active)
            counters.updated += 1
    session.commit()
    return counters.__dict__


def sync_transfers(session: Session, items: list[dict]) -> dict:
    counters = _SyncCounters()
    warehouses = {
        row.external_id: row.id for row in session.scalars(select(LogisticsWarehouse)).all()
    }
    for item in items:
        source_id = warehouses.get(item["source_warehouse_external_id"])
        target_id = warehouses.get(item["target_warehouse_external_id"])
        if source_id is None or target_id is None:
            raise _http_error(422, "transfer references unknown warehouse")
        row = session.scalar(
            select(LogisticsTransfer).where(LogisticsTransfer.external_id == item["external_id"])
        )
        created = False
        if row is None:
            row = LogisticsTransfer(
                external_id=item["external_id"],
                document_number=item["document_number"],
                document_date=_coerce_datetime(item["document_date"]),
                source_warehouse_id=source_id,
                target_warehouse_id=target_id,
                final_recipient_name=item.get("final_recipient_name"),
                barcode=item["barcode"],
                onec_status=item.get("status"),
                onec_deleted=item.get("onec_deleted", False),
                payload=item.get("payload"),
            )
            session.add(row)
            counters.created += 1
            created = True
        else:
            row.document_number = item["document_number"]
            row.document_date = _coerce_datetime(item["document_date"])
            row.source_warehouse_id = source_id
            row.target_warehouse_id = target_id
            row.final_recipient_name = item.get("final_recipient_name")
            row.barcode = item["barcode"]
            row.onec_status = item.get("status")
            row.onec_deleted = item.get("onec_deleted", False)
            row.payload = item.get("payload")
            counters.updated += 1
        session.flush()
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
