from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.customer_return import CustomerReturnShipment
from app.services import customer_returns as customer_return_service


def test_out_of_order_carrier_event_does_not_replace_current_state(db_session: Session) -> None:
    shipment, _ = customer_return_service.register_return(
        db_session,
        carrier="russian_post",
        tracking_number="12345678901234",
        source="manual",
    )
    current_event_at = datetime(2026, 9, 2, 10, 0, tzinfo=UTC)
    current_deadline = current_event_at + timedelta(days=5)
    shipment, _ = customer_return_service.record_carrier_event(
        db_session,
        shipment.id,
        status_code="READY_FOR_PICKUP",
        status_text="Можно забирать",
        occurred_at=current_event_at,
        external_event_id="current-arrival",
        storage_deadline_at=current_deadline,
    )

    shipment, event_created = customer_return_service.record_carrier_event(
        db_session,
        shipment.id,
        status_code="CANCELLED",
        status_text="Старое ошибочное состояние",
        occurred_at=current_event_at - timedelta(days=1),
        external_event_id="stale-cancellation",
        storage_deadline_at=current_deadline - timedelta(days=2),
    )

    assert event_created is True
    assert shipment.status == "arrived_at_pickup_point"
    assert customer_return_service._as_utc(shipment.status_changed_at) == current_event_at
    assert shipment.carrier_last_status_code == "READY_FOR_PICKUP"
    assert shipment.carrier_last_status_text == "Можно забирать"
    assert customer_return_service._as_utc(shipment.carrier_last_event_at) == current_event_at
    assert customer_return_service._as_utc(shipment.storage_deadline_at) == current_deadline
    assert [
        event.carrier_status_code
        for event in shipment.events
        if event.event_type == customer_return_service.EVENT_CARRIER_STATUS
    ] == [
        "CANCELLED",
        "READY_FOR_PICKUP",
    ]


def test_duplicate_registration_fills_missing_links(db_session: Session) -> None:
    shipment, created = customer_return_service.register_return(
        db_session,
        carrier="cdek",
        tracking_number="CDEK-3507",
        source="manual",
    )
    assert created is True

    shipment, created = customer_return_service.register_return(
        db_session,
        carrier="cdek",
        tracking_number="CDEK-3507",
        source="bitrix_ui",
        source_ref="task-3507:return-1",
        bitrix_case_id="CASE-3507",
        site_ticket_id="SITE-3507",
        onec_order_ref="ORDER-3507",
        created_by_bitrix_user_id="6357",
        payload={"registration": "enrichment"},
    )

    assert created is False
    assert shipment.source == "manual"
    assert shipment.source_ref == "task-3507:return-1"
    assert shipment.bitrix_case_id == "CASE-3507"
    assert shipment.site_ticket_id == "SITE-3507"
    assert shipment.onec_order_ref == "ORDER-3507"
    assert shipment.created_by_bitrix_user_id == "6357"
    assert shipment.source_payload == {"registration": "enrichment"}


class _RacingRegistrationSession:
    def __init__(self, winner: CustomerReturnShipment) -> None:
        self._scalar_results = iter((None, winner, winner))
        self.rolled_back = False
        self.committed = False

    def scalar(self, _statement):
        return next(self._scalar_results)

    def add(self, _instance) -> None:
        return None

    def flush(self) -> None:
        raise IntegrityError("INSERT customer_return_shipment", {}, Exception("duplicate"))

    def rollback(self) -> None:
        self.rolled_back = True

    def commit(self) -> None:
        self.committed = True


def test_concurrent_duplicate_registration_returns_winning_row() -> None:
    now = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    winner = CustomerReturnShipment(
        id=3507,
        carrier="cdek",
        tracking_number="CDEK-RACE-3507",
        status="registered",
        status_changed_at=now,
        source="site",
        updated_at=now,
    )
    racing_session = _RacingRegistrationSession(winner)

    shipment, created = customer_return_service.register_return(
        racing_session,  # type: ignore[arg-type]
        carrier="cdek",
        tracking_number="CDEK-RACE-3507",
        source="bitrix_ui",
        onec_order_ref="ORDER-3507",
    )

    assert created is False
    assert shipment is winner
    assert shipment.onec_order_ref == "ORDER-3507"
    assert racing_session.rolled_back is True
    assert racing_session.committed is True
