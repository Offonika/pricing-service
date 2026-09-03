from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.models.customer_return import (
    CustomerReturnAction,
    CustomerReturnEvent,
    CustomerReturnShipment,
)
from app.models.expertise import ExpertiseCase, ExpertiseCaseEvent
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
EVENT_DEAL_LINK_CHANGED = "deal_link_changed"
EVENT_SERVICE_REQUEST_LINK_CHANGED = "service_request_link_changed"

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


@dataclass(frozen=True, slots=True)
class CustomerReturnDealLink:
    deal_id: int
    title: str
    order_ref: str | None = None
    stage_id: str | None = None
    stage_name: str | None = None
    closed: bool = False
    created_at: datetime | None = None
    contact_id: int | None = None
    contact_name: str | None = None
    company_id: int | None = None
    company_name: str | None = None
    responsible_user_id: int | None = None
    responsible_name: str | None = None


@dataclass(frozen=True, slots=True)
class CustomerReturnServiceRequestLink:
    item_id: int
    title: str
    stage_id: str | None = None
    stage_name: str | None = None
    closed: bool = False
    category_id: int | None = None
    deal_id: int | None = None
    order_ref: str | None = None
    responsible_user_id: int | None = None
    responsible_name: str | None = None
    site_ticket_id: str | None = None


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
    without_service_request: bool | None = None,
    limit: int = 100,
) -> list[CustomerReturnShipment]:
    statement = select(CustomerReturnShipment)
    if carrier:
        statement = statement.where(CustomerReturnShipment.carrier == carrier)
    if status:
        statement = statement.where(CustomerReturnShipment.status == status)
    if without_service_request is True:
        statement = statement.where(CustomerReturnShipment.service_request_item_id.is_(None))
    elif without_service_request is False:
        statement = statement.where(CustomerReturnShipment.service_request_item_id.is_not(None))
    statement = statement.order_by(
        CustomerReturnShipment.updated_at.desc(), CustomerReturnShipment.id.desc()
    ).limit(limit)
    return list(db.scalars(statement).all())


def attach_expertise_cases(
    db: Session,
    shipments: CustomerReturnShipment | list[CustomerReturnShipment],
) -> CustomerReturnShipment | list[CustomerReturnShipment]:
    rows = shipments if isinstance(shipments, list) else [shipments]
    request_ids = {
        row.service_request_item_id for row in rows if row.service_request_item_id is not None
    }
    expertise_by_request: dict[int, list[ExpertiseCase]] = {
        request_id: [] for request_id in request_ids
    }
    if request_ids:
        cases = db.scalars(
            select(ExpertiseCase)
            .where(ExpertiseCase.service_request_item_id.in_(request_ids))
            .order_by(ExpertiseCase.created_at.desc())
        ).all()
        for case in cases:
            if case.service_request_item_id is not None:
                expertise_by_request.setdefault(case.service_request_item_id, []).append(case)
    for row in rows:
        row.__dict__["_customer_return_expertise_cases"] = expertise_by_request.get(
            row.service_request_item_id or 0, []
        )
    return shipments


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


def _deal_link_payload(link: CustomerReturnDealLink | None) -> dict | None:
    if link is None:
        return None
    return {
        "deal_id": link.deal_id,
        "title": link.title,
        "order_ref": link.order_ref,
    }


def _apply_deal_link(
    shipment: CustomerReturnShipment,
    link: CustomerReturnDealLink,
    *,
    actor_bitrix_user_id: str,
    linked_at: datetime,
) -> None:
    shipment.bitrix_deal_id = link.deal_id
    shipment.bitrix_deal_title = link.title
    shipment.bitrix_order_ref = link.order_ref
    shipment.bitrix_deal_stage_id = link.stage_id
    shipment.bitrix_deal_stage_name = link.stage_name
    shipment.bitrix_deal_closed = link.closed
    shipment.bitrix_contact_id = link.contact_id
    shipment.bitrix_contact_name = link.contact_name
    shipment.bitrix_company_id = link.company_id
    shipment.bitrix_company_name = link.company_name
    shipment.bitrix_responsible_user_id = link.responsible_user_id
    shipment.bitrix_responsible_name = link.responsible_name
    shipment.bitrix_deal_linked_at = linked_at
    shipment.bitrix_deal_linked_by_user_id = actor_bitrix_user_id
    shipment.onec_order_ref = link.order_ref


def _clear_deal_link(shipment: CustomerReturnShipment) -> None:
    shipment.bitrix_deal_id = None
    shipment.bitrix_deal_title = None
    shipment.bitrix_order_ref = None
    shipment.bitrix_deal_stage_id = None
    shipment.bitrix_deal_stage_name = None
    shipment.bitrix_deal_closed = None
    shipment.bitrix_contact_id = None
    shipment.bitrix_contact_name = None
    shipment.bitrix_company_id = None
    shipment.bitrix_company_name = None
    shipment.bitrix_responsible_user_id = None
    shipment.bitrix_responsible_name = None
    shipment.bitrix_deal_linked_at = None
    shipment.bitrix_deal_linked_by_user_id = None


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
    deal_link: CustomerReturnDealLink | None,
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
    if deal_link is not None:
        if shipment.bitrix_deal_id not in (None, deal_link.deal_id):
            raise CustomerReturnConflict(
                "customer return is already linked to another Bitrix24 deal"
            )
        if shipment.bitrix_deal_id is None:
            linked_at = _utcnow()
            _apply_deal_link(
                shipment,
                deal_link,
                actor_bitrix_user_id=created_by_bitrix_user_id or "system",
                linked_at=linked_at,
            )
            db.add(
                CustomerReturnEvent(
                    shipment_id=shipment.id,
                    event_type=EVENT_DEAL_LINK_CHANGED,
                    source="bitrix24",
                    dedupe_key=_dedupe_key(
                        "deal-link",
                        shipment.id,
                        "none",
                        deal_link.deal_id,
                    ),
                    actor_bitrix_user_id=created_by_bitrix_user_id,
                    occurred_at=linked_at,
                    payload={"old": None, "new": _deal_link_payload(deal_link)},
                )
            )
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
    deal_link: CustomerReturnDealLink | None = None,
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
                deal_link=deal_link,
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
    if deal_link is not None:
        _apply_deal_link(
            shipment,
            deal_link,
            actor_bitrix_user_id=created_by_bitrix_user_id or "system",
            linked_at=now,
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
                payload={
                    **(payload or {}),
                    **(
                        {"deal_link": _deal_link_payload(deal_link)}
                        if deal_link is not None
                        else {}
                    ),
                }
                or None,
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
                    deal_link=deal_link,
                ),
                False,
            )
        raise CustomerReturnConflict(
            "customer return registration conflicts with existing data"
        ) from exc
    return get_return(db, shipment.id), True


def update_return_deal_link(
    db: Session,
    shipment_id: int,
    *,
    deal_link: CustomerReturnDealLink | None,
    actor_bitrix_user_id: str,
) -> CustomerReturnShipment:
    shipment = get_return(db, shipment_id)
    old_deal_id = shipment.bitrix_deal_id
    new_deal_id = deal_link.deal_id if deal_link is not None else None
    if (
        shipment.service_request_item_id is not None
        and shipment.service_request_deal_id is not None
        and new_deal_id != shipment.service_request_deal_id
    ):
        raise CustomerReturnConflict(
            "remove or replace the service request before changing its Bitrix24 deal"
        )
    if old_deal_id == new_deal_id:
        return shipment

    old_payload = (
        {
            "deal_id": shipment.bitrix_deal_id,
            "title": shipment.bitrix_deal_title,
            "order_ref": shipment.bitrix_order_ref,
        }
        if shipment.bitrix_deal_id is not None
        else None
    )
    occurred_at = _utcnow()
    if deal_link is None:
        _clear_deal_link(shipment)
    else:
        _apply_deal_link(
            shipment,
            deal_link,
            actor_bitrix_user_id=actor_bitrix_user_id,
            linked_at=occurred_at,
        )
    shipment.updated_at = occurred_at
    db.add(
        CustomerReturnEvent(
            shipment_id=shipment.id,
            event_type=EVENT_DEAL_LINK_CHANGED,
            source="bitrix24",
            dedupe_key=_dedupe_key(
                "deal-link",
                shipment.id,
                old_deal_id or "none",
                new_deal_id or "none",
                occurred_at.isoformat(),
            ),
            actor_bitrix_user_id=actor_bitrix_user_id,
            occurred_at=occurred_at,
            payload={"old": old_payload, "new": _deal_link_payload(deal_link)},
        )
    )
    db.commit()
    return get_return(db, shipment.id)


def _service_request_payload(
    link: CustomerReturnServiceRequestLink | None,
) -> dict | None:
    if link is None:
        return None
    return {
        "item_id": link.item_id,
        "title": link.title,
        "deal_id": link.deal_id,
        "order_ref": link.order_ref,
        "site_ticket_id": link.site_ticket_id,
    }


def _apply_service_request_link(
    shipment: CustomerReturnShipment,
    link: CustomerReturnServiceRequestLink,
    *,
    actor_bitrix_user_id: str,
    linked_at: datetime,
) -> None:
    shipment.service_request_item_id = link.item_id
    shipment.service_request_title = link.title
    shipment.service_request_stage_id = link.stage_id
    shipment.service_request_stage_name = link.stage_name
    shipment.service_request_closed = link.closed
    shipment.service_request_deal_id = link.deal_id
    shipment.service_request_order_ref = link.order_ref
    shipment.service_request_responsible_user_id = link.responsible_user_id
    shipment.service_request_responsible_name = link.responsible_name
    shipment.service_request_linked_at = linked_at
    shipment.service_request_linked_by_user_id = actor_bitrix_user_id
    shipment.bitrix_case_id = str(link.item_id)
    shipment.site_ticket_id = link.site_ticket_id


def _clear_service_request_link(shipment: CustomerReturnShipment) -> None:
    shipment.service_request_item_id = None
    shipment.service_request_title = None
    shipment.service_request_stage_id = None
    shipment.service_request_stage_name = None
    shipment.service_request_closed = None
    shipment.service_request_deal_id = None
    shipment.service_request_order_ref = None
    shipment.service_request_responsible_user_id = None
    shipment.service_request_responsible_name = None
    shipment.service_request_linked_at = None
    shipment.service_request_linked_by_user_id = None
    shipment.bitrix_case_id = None
    shipment.site_ticket_id = None


def update_return_service_request_link(
    db: Session,
    shipment_id: int,
    *,
    service_request_link: CustomerReturnServiceRequestLink | None,
    actor_bitrix_user_id: str,
    deal_link_if_missing: CustomerReturnDealLink | None = None,
) -> CustomerReturnShipment:
    shipment = get_return(db, shipment_id)
    old_item_id = shipment.service_request_item_id
    new_item_id = service_request_link.item_id if service_request_link else None
    if service_request_link is not None:
        if (
            shipment.bitrix_deal_id is not None
            and service_request_link.deal_id is not None
            and shipment.bitrix_deal_id != service_request_link.deal_id
        ):
            raise CustomerReturnConflict("service request belongs to another Bitrix24 deal")
        if shipment.bitrix_deal_id is None and service_request_link.deal_id is not None:
            if (
                deal_link_if_missing is None
                or deal_link_if_missing.deal_id != service_request_link.deal_id
            ):
                raise CustomerReturnConflict(
                    "trusted Bitrix24 deal snapshot is required for service request"
                )

    occurred_at = _utcnow()
    if old_item_id == new_item_id:
        if service_request_link is not None:
            _apply_service_request_link(
                shipment,
                service_request_link,
                actor_bitrix_user_id=actor_bitrix_user_id,
                linked_at=shipment.service_request_linked_at or occurred_at,
            )
            shipment.updated_at = occurred_at
            db.commit()
        return get_return(db, shipment.id)

    old_payload = (
        {
            "item_id": old_item_id,
            "title": shipment.service_request_title,
            "deal_id": shipment.service_request_deal_id,
            "order_ref": shipment.service_request_order_ref,
            "site_ticket_id": shipment.site_ticket_id,
        }
        if old_item_id is not None
        else None
    )
    if service_request_link is None:
        _clear_service_request_link(shipment)
    else:
        if shipment.bitrix_deal_id is None and deal_link_if_missing is not None:
            _apply_deal_link(
                shipment,
                deal_link_if_missing,
                actor_bitrix_user_id=actor_bitrix_user_id,
                linked_at=occurred_at,
            )
            db.add(
                CustomerReturnEvent(
                    shipment_id=shipment.id,
                    event_type=EVENT_DEAL_LINK_CHANGED,
                    source="bitrix24",
                    dedupe_key=_dedupe_key(
                        "deal-link-from-service-request",
                        shipment.id,
                        deal_link_if_missing.deal_id,
                    ),
                    actor_bitrix_user_id=actor_bitrix_user_id,
                    occurred_at=occurred_at,
                    payload={"old": None, "new": _deal_link_payload(deal_link_if_missing)},
                )
            )
        _apply_service_request_link(
            shipment,
            service_request_link,
            actor_bitrix_user_id=actor_bitrix_user_id,
            linked_at=occurred_at,
        )
    shipment.updated_at = occurred_at
    db.add(
        CustomerReturnEvent(
            shipment_id=shipment.id,
            event_type=EVENT_SERVICE_REQUEST_LINK_CHANGED,
            source="bitrix24",
            dedupe_key=_dedupe_key(
                "service-request-link",
                shipment.id,
                old_item_id or "none",
                new_item_id or "none",
                occurred_at.isoformat(),
            ),
            actor_bitrix_user_id=actor_bitrix_user_id,
            occurred_at=occurred_at,
            payload={
                "old": old_payload,
                "new": _service_request_payload(service_request_link),
            },
        )
    )
    db.commit()
    return get_return(db, shipment.id)


def list_customer_return_expertise(
    db: Session,
    *,
    service_request_item_id: int | None = None,
    search: str | None = None,
    limit: int = 20,
) -> list[ExpertiseCase]:
    statement = select(ExpertiseCase)
    if service_request_item_id is not None:
        statement = statement.where(
            ExpertiseCase.service_request_item_id == service_request_item_id
        )
    query = (search or "").strip()
    if query:
        pattern = f"%{query}%"
        statement = statement.where(
            or_(
                ExpertiseCase.external_id.ilike(pattern),
                ExpertiseCase.onec_expertise_number.ilike(pattern),
                ExpertiseCase.linked_customer_order_number.ilike(pattern),
            )
        )
    return list(
        db.scalars(
            statement.order_by(ExpertiseCase.updated_at.desc(), ExpertiseCase.id.desc()).limit(
                limit
            )
        ).all()
    )


def _normalized_order(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = "".join(char for char in value if char.isalnum()).casefold()
    return normalized or None


def update_expertise_service_request_link(
    db: Session,
    case_id: int,
    *,
    service_request_link: CustomerReturnServiceRequestLink | None,
    actor_bitrix_user_id: str,
) -> ExpertiseCase:
    case = db.get(ExpertiseCase, case_id)
    if case is None:
        raise CustomerReturnNotFound("expertise case not found")
    old_item_id = case.service_request_item_id
    new_item_id = service_request_link.item_id if service_request_link else None
    if old_item_id == new_item_id:
        return case
    expertise_order = _normalized_order(case.linked_customer_order_number)
    known_request_orders = {
        order
        for value in (
            service_request_link.order_ref if service_request_link else None,
            *(
                db.scalars(
                    select(CustomerReturnShipment.bitrix_order_ref).where(
                        CustomerReturnShipment.service_request_item_id == new_item_id
                    )
                ).all()
                if new_item_id is not None
                else []
            ),
        )
        if (order := _normalized_order(value)) is not None
    }
    if expertise_order and any(expertise_order != order for order in known_request_orders):
        raise CustomerReturnConflict(
            "expertise case and service request have different order numbers"
        )
    occurred_at = _utcnow()
    case.service_request_item_id = new_item_id
    case.service_request_linked_at = occurred_at if new_item_id is not None else None
    case.service_request_linked_by_user_id = (
        actor_bitrix_user_id if new_item_id is not None else None
    )
    db.add(
        ExpertiseCaseEvent(
            expertise_case_id=case.id,
            event_type=EVENT_SERVICE_REQUEST_LINK_CHANGED,
            event_at=occurred_at,
            actor_external_id=actor_bitrix_user_id,
            source="bitrix24",
            idempotency_key=_dedupe_key(
                "expertise-service-request-link",
                case.id,
                old_item_id or "none",
                new_item_id or "none",
                occurred_at.isoformat(),
            ),
            meta={
                "old_service_request_item_id": old_item_id,
                "new_service_request_item_id": new_item_id,
                "order_match_checked": bool(expertise_order and known_request_orders),
            },
        )
    )
    db.commit()
    db.refresh(case)
    return case


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
