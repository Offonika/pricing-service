from __future__ import annotations

from sqlalchemy import select

from app.models.procurement_order_formation import ProcurementOrderFormationEvent
from tasks.reconcile_procurement_order_missing_number import reconcile_order
from tests.test_procurement_order_formation import _order


def test_reconciliation_task_is_dry_run_by_default(db_session) -> None:
    order = _order(db_session)
    order.status = "transmitted"
    order.onec_status = "transmitted"
    db_session.commit()

    result = reconcile_order(db_session, order_id=order.id, apply=False)

    db_session.refresh(order)
    assert result["changed"] is True
    assert order.status == "transmitted"
    assert db_session.scalar(select(ProcurementOrderFormationEvent)) is None


def test_reconciliation_task_applies_once_and_records_audit(db_session) -> None:
    order = _order(db_session)
    order.status = "transmitted"
    order.onec_status = "transmitted"
    db_session.commit()

    result = reconcile_order(db_session, order_id=order.id, apply=True)

    db_session.refresh(order)
    event = db_session.scalar(select(ProcurementOrderFormationEvent))
    assert result["changed"] is True
    assert order.status == "error"
    assert order.onec_status == "error"
    assert event is not None
    assert event.event_type == "onec_number_reconciliation_required"

    repeated = reconcile_order(db_session, order_id=order.id, apply=True)
    assert repeated["changed"] is False
