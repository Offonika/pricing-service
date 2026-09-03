from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services import order_assembly_outbox as service


def _payload(**overrides) -> service.AssemblyOutboxInput:
    values = {
        "event_key": "assembled-order:order-1:20260903120000",
        "event_at": datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc),
        "assembly_source": "customer_order",
        "assembly_ref": "order-1",
        "site_order_number": "218530",
        "execution_status": "06",
        "delivery_code": "MM_COURIER",
        "payment_mode": "by_agreement",
        "onec_order_number": "РБГУ0033819",
    }
    values.update(overrides)
    return service.AssemblyOutboxInput(**values)


def test_enqueue_is_idempotent_and_preserves_onec_order_case(db_session) -> None:
    first = service.enqueue_assembly_event(db_session, _payload())
    db_session.commit()
    second = service.enqueue_assembly_event(db_session, _payload())

    assert first.created is True
    assert second.created is False
    assert second.row.id == first.row.id
    assert second.row.onec_order_number == "РБГУ0033819"
    assert second.row.status == "pending"


def test_same_event_key_with_other_payload_is_rejected(db_session) -> None:
    service.enqueue_assembly_event(db_session, _payload())
    db_session.commit()

    with pytest.raises(service.AssemblyOutboxConflict):
        service.enqueue_assembly_event(
            db_session,
            _payload(site_order_number="218531"),
        )


def test_mm_courier_requires_status_06_but_accepts_payment_on_receipt(db_session) -> None:
    accepted = service.enqueue_assembly_event(db_session, _payload())
    assert accepted.row.payment_mode == "by_agreement"

    with pytest.raises(service.AssemblyOutboxError, match="execution_status=06"):
        service.enqueue_assembly_event(
            db_session,
            _payload(event_key="assembled-order:order-2", execution_status="05"),
        )


def test_failed_delivery_retries_then_moves_to_manual_review(db_session) -> None:
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    row = service.enqueue_assembly_event(db_session, _payload(), now=now).row
    db_session.commit()

    first = service.deliver_due_events(
        db_session,
        sender=lambda _row: {"ok": False, "error": "CRM unavailable"},
        now=now,
        max_attempts=2,
        retry_base_seconds=30,
    )
    db_session.commit()
    assert first == {"selected": 1, "delivered": 0, "retry": 1, "manual_review": 0}
    assert row.status == "retry"
    assert row.next_attempt_at.replace(tzinfo=timezone.utc) == now + timedelta(seconds=30)

    second = service.deliver_due_events(
        db_session,
        sender=lambda _row: {"ok": False, "error": "still unavailable"},
        now=now + timedelta(seconds=30),
        max_attempts=2,
        retry_base_seconds=30,
    )
    assert second == {"selected": 1, "delivered": 0, "retry": 0, "manual_review": 1}
    assert row.status == "manual_review"
    assert row.attempt_count == 2


def test_crm_payload_contains_delivery_contract(db_session) -> None:
    row = service.enqueue_assembly_event(db_session, _payload()).row

    payload = service.crm_payload(row)

    assert payload["execution_status"] == "06"
    assert payload["delivery_code"] == "MM_COURIER"
    assert payload["payment_mode"] == "by_agreement"
    assert payload["onec_order_number"] == "РБГУ0033819"
