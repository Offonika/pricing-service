from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select

from app.models import LogisticsManualReview, SiteOrderExecutionEvent, SiteOrderStageOutbox
from app.services import site_order_execution_reconciliation as reconciliation
from app.services import site_order_fulfillment as fulfillment


def _snapshot(**overrides) -> reconciliation.ExecutionEvidenceSnapshot:
    values = {
        "site_order_number": "242901",
        "bitrix_deal_id": 39001,
        "current_stage": "EXECUTING",
        "delivery_class": fulfillment.DELIVERY_CLASS_PICKUP,
        "raw_delivery": "Самовывоз",
        "duplicate_deal_ids": (39001,),
        "rtu_count": 1,
        "assembled_rtu_count": 1,
        "posted_sale_amount": Decimal("1500.00"),
        "latest_rtu_at": datetime(2026, 8, 26, 10, 0),
        "latest_assembled_at": datetime(2026, 8, 26, 10, 5),
    }
    values.update(overrides)
    return reconciliation.ExecutionEvidenceSnapshot(**values)


def test_assembled_orders_leave_executing_by_delivery_class() -> None:
    pickup = reconciliation.decide_execution_stage(_snapshot())
    courier = reconciliation.decide_execution_stage(
        _snapshot(
            delivery_class=fulfillment.DELIVERY_CLASS_COURIER,
            raw_delivery="Доставка курьером",
        )
    )
    unpaid_carrier = reconciliation.decide_execution_stage(
        _snapshot(
            delivery_class=fulfillment.DELIVERY_CLASS_CARRIER,
            raw_delivery="СДЭК",
        )
    )
    paid_carrier = reconciliation.decide_execution_stage(
        _snapshot(
            delivery_class=fulfillment.DELIVERY_CLASS_CARRIER,
            raw_delivery="Почта России",
            onec_payment_confirmed=True,
        )
    )

    assert pickup.target_stage == "FINAL_INVOICE"
    assert courier.target_stage == "FINAL_INVOICE"
    assert unpaid_carrier.target_stage == "PREPAYMENT_INVOICE"
    assert paid_carrier.target_stage == "FINAL_INVOICE"


def test_print_and_scan_closes_only_internal_pickup() -> None:
    pickup = reconciliation.decide_execution_stage(
        _snapshot(issued_rtu_count=1, latest_issued_at=datetime(2026, 8, 26, 11, 0))
    )
    carrier = reconciliation.decide_execution_stage(
        _snapshot(
            delivery_class=fulfillment.DELIVERY_CLASS_CARRIER,
            raw_delivery="СДЭК",
            issued_rtu_count=1,
            latest_issued_at=datetime(2026, 8, 26, 11, 0),
        )
    )

    assert pickup.target_stage == "WON"
    assert pickup.reason == "pickup_printed_and_scanned"
    assert carrier.action == reconciliation.ACTION_MANUAL_REVIEW
    assert carrier.reason == "issued_rtu_not_pickup_handoff"


def test_partial_multi_rtu_assembly_or_issue_never_advances_whole_order() -> None:
    partial_assembly = reconciliation.decide_execution_stage(
        _snapshot(rtu_count=5, assembled_rtu_count=4)
    )
    partial_issue = reconciliation.decide_execution_stage(
        _snapshot(rtu_count=5, assembled_rtu_count=4, issued_rtu_count=4)
    )
    all_issued = reconciliation.decide_execution_stage(
        _snapshot(rtu_count=5, assembled_rtu_count=5, issued_rtu_count=5)
    )

    assert partial_assembly.action == reconciliation.ACTION_MANUAL_REVIEW
    assert partial_assembly.reason == "partial_rtu_assembly"
    assert partial_issue.action == reconciliation.ACTION_MANUAL_REVIEW
    assert partial_issue.reason == "partial_rtu_issue"
    assert all_issued.action == reconciliation.ACTION_UPDATE_STAGE
    assert all_issued.target_stage == "WON"


def test_item_coverage_blocks_order_even_when_all_existing_rtus_are_assembled() -> None:
    partial = reconciliation.decide_execution_stage(
        _snapshot(
            rtu_count=1,
            assembled_rtu_count=1,
            line_coverage_status="partial",
            expected_item_quantity=Decimal("3"),
            assembled_item_quantity=Decimal("1"),
            missing_item_count=1,
        )
    )
    complete = reconciliation.decide_execution_stage(
        _snapshot(
            rtu_count=2,
            assembled_rtu_count=2,
            line_coverage_status="complete",
            expected_item_quantity=Decimal("3"),
            assembled_item_quantity=Decimal("3"),
        )
    )

    assert partial.action == reconciliation.ACTION_NOOP
    assert partial.reason == "waiting_for_full_order_item_assembly"
    assert complete.target_stage == "FINAL_INVOICE"


def test_item_coverage_excess_or_unavailable_fails_closed() -> None:
    conflict = reconciliation.decide_execution_stage(
        _snapshot(line_coverage_status="conflict", excess_item_count=1)
    )
    unavailable = reconciliation.decide_execution_stage(
        _snapshot(line_coverage_status="unavailable")
    )

    assert conflict.reason == "assembly_line_quantity_conflict"
    assert unavailable.reason == "assembly_line_coverage_unavailable"
    assert conflict.action == reconciliation.ACTION_MANUAL_REVIEW
    assert unavailable.action == reconciliation.ACTION_MANUAL_REVIEW


def test_impossible_rtu_evidence_counts_are_blocked() -> None:
    decision = reconciliation.decide_execution_stage(_snapshot(rtu_count=1, assembled_rtu_count=2))

    assert decision.action == reconciliation.ACTION_MANUAL_REVIEW
    assert decision.reason == "rtu_evidence_count_mismatch"


def test_return_rules_fail_closed_for_payment_partial_or_issue_conflicts() -> None:
    full_unpaid = reconciliation.decide_execution_stage(
        _snapshot(
            returned_rtu_count=1,
            returned_amount=Decimal("1500.00"),
            latest_return_at=datetime(2026, 8, 26, 12, 0),
        )
    )
    paid = reconciliation.decide_execution_stage(
        _snapshot(
            returned_rtu_count=1,
            returned_amount=Decimal("1500.00"),
            latest_return_at=datetime(2026, 8, 26, 12, 0),
            onec_payment_confirmed=True,
        )
    )
    partial = reconciliation.decide_execution_stage(
        _snapshot(
            returned_rtu_count=1,
            returned_amount=Decimal("500.00"),
            latest_return_at=datetime(2026, 8, 26, 12, 0),
        )
    )
    issued_then_returned = reconciliation.decide_execution_stage(
        _snapshot(
            issued_rtu_count=1,
            returned_rtu_count=1,
            returned_amount=Decimal("1500.00"),
            latest_issued_at=datetime(2026, 8, 26, 11, 0),
            latest_return_at=datetime(2026, 8, 26, 12, 0),
        )
    )

    assert full_unpaid.target_stage == "LOSE"
    assert paid.reason == "paid_and_returned"
    assert partial.reason == "partial_or_unquantified_return"
    assert issued_then_returned.reason == "issued_and_returned"
    assert all(
        item.action == reconciliation.ACTION_MANUAL_REVIEW
        for item in (paid, partial, issued_then_returned)
    )


def test_canceled_before_fulfillment_requires_inactive_marked_onec_order() -> None:
    canceled_before_fulfillment = _snapshot(
        site_canceled=True,
        onec_order_count=1,
        onec_inactive_marked_order_count=1,
        rtu_count=0,
        assembled_rtu_count=0,
        posted_sale_amount=None,
        latest_rtu_at=None,
        latest_assembled_at=None,
    )

    safe = reconciliation.decide_execution_stage(canceled_before_fulfillment)
    active_onec_order = reconciliation.decide_execution_stage(
        replace(canceled_before_fulfillment, onec_inactive_marked_order_count=0)
    )
    paid = reconciliation.decide_execution_stage(
        replace(canceled_before_fulfillment, site_paid=True)
    )
    assembled = reconciliation.decide_execution_stage(
        replace(canceled_before_fulfillment, crm_assembled=True)
    )
    with_rtu = reconciliation.decide_execution_stage(
        replace(
            canceled_before_fulfillment,
            rtu_count=1,
            latest_rtu_at=datetime(2026, 8, 26, 10, 0),
        )
    )

    assert safe.action == reconciliation.ACTION_UPDATE_STAGE
    assert safe.target_stage == "LOSE"
    assert safe.reason == "canceled_before_fulfillment"
    assert all(
        item.action == reconciliation.ACTION_MANUAL_REVIEW
        for item in (active_onec_order, paid, assembled, with_rtu)
    )
    assert all(
        item.reason == "canceled_without_confirmed_return"
        for item in (active_onec_order, paid, assembled, with_rtu)
    )


def test_duplicate_terminal_and_missing_onec_evidence_never_change_stage() -> None:
    duplicate = reconciliation.decide_execution_stage(_snapshot(duplicate_deal_ids=(39001, 39002)))
    terminal = reconciliation.decide_execution_stage(_snapshot(current_stage="WON"))
    unavailable = reconciliation.decide_execution_stage(_snapshot(onec_evidence_available=False))

    assert duplicate.action == reconciliation.ACTION_MANUAL_REVIEW
    assert duplicate.reason == "multiple_bitrix_deals"
    assert terminal.action == reconciliation.ACTION_NOOP
    assert unavailable.action == reconciliation.ACTION_MANUAL_REVIEW
    assert unavailable.reason == "onec_evidence_unavailable"


def test_persistence_is_append_only_and_outbox_is_idempotent(db_session) -> None:
    snapshot = _snapshot()
    decision = reconciliation.decide_execution_stage(snapshot)

    first = reconciliation.persist_execution_decision(
        db_session,
        snapshot=snapshot,
        decision=decision,
    )
    second = reconciliation.persist_execution_decision(
        db_session,
        snapshot=snapshot,
        decision=decision,
    )
    db_session.commit()

    assert first.result == "outbox_created"
    assert second.result == "duplicate_snapshot"
    events = db_session.scalars(select(SiteOrderExecutionEvent)).all()
    outbox = db_session.scalars(select(SiteOrderStageOutbox)).all()
    assert len(events) == 1
    assert events[0].source == "onec"
    assert len(outbox) == 1
    assert outbox[0].target_stage == "FINAL_INVOICE"
    metrics = reconciliation.execution_reconciliation_metrics(db_session)
    assert metrics["execution_event_count"] == 1
    assert metrics["execution_outbox_active"] == 1
    assert metrics["execution_outbox_by_status"] == {"pending": 1}


def test_older_onec_snapshot_does_not_supersede_newer_execution_event(db_session) -> None:
    newer = fulfillment.upsert_execution_event(
        db_session,
        site_order_number="242901",
        event_type="pickup_stored_at_point",
        event_at=datetime(2026, 8, 26, 15, 0),
        source="bitrix_chat",
        source_ref="chat8729:9001",
        confidence="strong",
        raw_message_id=None,
        payload={},
    )
    assert newer is not None

    snapshot = replace(
        _snapshot(),
        latest_rtu_at=datetime(2026, 8, 26, 10, 0),
        latest_assembled_at=datetime(2026, 8, 26, 10, 5),
    )
    result = reconciliation.persist_execution_decision(
        db_session,
        snapshot=snapshot,
        decision=reconciliation.decide_execution_stage(snapshot),
    )
    db_session.commit()

    assert result.result == "stale_evidence"
    assert db_session.scalars(select(SiteOrderStageOutbox)).all() == []


def test_new_strict_evidence_resolves_previous_manual_review(db_session) -> None:
    unresolved = _snapshot(
        assembled_rtu_count=0,
        latest_assembled_at=None,
    )
    manual = reconciliation.persist_execution_decision(
        db_session,
        snapshot=unresolved,
        decision=reconciliation.decide_execution_stage(unresolved),
    )
    assert manual.result == reconciliation.ACTION_MANUAL_REVIEW

    assembled = replace(
        unresolved,
        assembled_rtu_count=1,
        latest_assembled_at=datetime(2026, 8, 26, 11, 0),
    )
    applied = reconciliation.persist_execution_decision(
        db_session,
        snapshot=assembled,
        decision=reconciliation.decide_execution_stage(assembled),
    )
    db_session.commit()

    assert applied.result == "outbox_created"
    review = db_session.scalar(select(LogisticsManualReview))
    assert review is not None
    assert review.status == "resolved"
    assert review.resolved_at is not None
