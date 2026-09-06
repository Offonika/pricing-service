from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.services.procurement_exceptions import reaction_deadline
from app.services.procurement_order_registry import lifecycle_status_for_snapshot
from app.services.procurement_receipt_evidence import attach_receipt_evidence
from app.services.procurement_supply_scenarios import annotate_scenario, partition_supply


@pytest.mark.parametrize(
    ("detected", "expected"),
    [
        ("2026-09-04T09:00:00", "2026-09-07T15:00:00"),
        ("2026-06-11T09:00:00", "2026-06-15T15:00:00"),
        ("2026-11-03T09:00:00", "2026-11-05T15:00:00"),
    ],
)
def test_reaction_deadline_uses_russian_holidays(detected, expected):
    assert reaction_deadline(
        datetime.fromisoformat(detected).replace(tzinfo=UTC)
    ) == datetime.fromisoformat(expected)


def test_working_weekend_override():
    calendar = {"years": {"2026": {"holidays": [], "working_weekends": ["2026-09-05"]}}}
    assert reaction_deadline(datetime(2026, 9, 4), calendar=calendar) == datetime(2026, 9, 5, 15)


def test_unknown_calendar_is_not_invented():
    with pytest.raises(ValueError, match="2027"):
        reaction_deadline(datetime(2026, 12, 31))


def test_supply_categories_and_horizon_boundaries():
    result = partition_supply(
        [
            {"quantity": 2, "expected_at": "2026-09-04"},
            {"quantity": 3, "expected_at": None},
            {"quantity": 5, "expected_at": "2026-09-05"},
            {"quantity": 7, "expected_at": "2026-09-15"},
            {"quantity": 11, "expected_at": "2026-09-16"},
        ],
        as_of=date(2026, 9, 5),
        horizon_days=10,
    )
    assert result["dated_quantity"] == "12"
    assert result["overdue_quantity"] == "2"
    assert result["undated_quantity"] == "3"
    assert result["later_quantity"] == "11"
    assert result["nearest_expected_at"] == "2026-09-05"


@pytest.mark.parametrize("decision", ["order", "do_not_order"])
def test_guard_does_not_apply_advisory_quantity_and_is_idempotent(decision):
    row = dict(
        assortment_status="sale",
        dry_run_decision=decision,
        avg_daily_sales_qty="2",
        sellable_stock_qty="10",
        lead_time_days="20",
        distribution_to_shelf_days="3",
        recommended_order_qty="6",
        incoming_qty="50",
        effective_target_days="30",
    )
    annotate_scenario(row, "56", as_of=date(2026, 9, 5))
    first = dict(row)
    annotate_scenario(row, "56", as_of=date(2026, 9, 5))
    assert row == first
    assert row["recommended_order_qty"] == "6"
    assert row["stockout_guard_triggered"] == "true"
    assert row["supply_review_required"] == "true"


@pytest.mark.parametrize("open_qty", [0, 6])
def test_closed_or_reduced_obligation_is_not_a_receipt(open_qty):
    snapshot = {
        "posted": True,
        "ordered_qty": 10,
        "open_qty": open_qty,
        "lines": [{"item_ref_hex": "0xabc", "quantity": 10}],
    }
    attach_receipt_evidence(snapshot, [])
    assert snapshot["received_qty"] == "0"
    assert lifecycle_status_for_snapshot(snapshot) == "reconciliation_required"


def test_receipt_duplicate_sku_does_not_duplicate_execution():
    snapshot = {
        "lines": [
            {"item_ref_hex": "0xabc", "quantity": 4},
            {"item_ref_hex": "0xabc", "quantity": 6},
        ]
    }
    attach_receipt_evidence(
        snapshot, [{"item_ref": "0xabc", "receipt_ref": "0x123", "quantity": "7"}]
    )
    assert Decimal(snapshot["received_qty"]) == 7
    assert not snapshot["receipt_evidence"]["fulfillment_complete"]
    assert all(line["received_quantity"] is None for line in snapshot["lines"])
    assert snapshot["receipt_evidence"]["return_quantity"] is None


def test_receipt_source_failure_preserves_lifecycle():
    snapshot = {
        "posted": True,
        "ordered_qty": 10,
        "open_qty": 0,
        "receipt_evidence": {"status": "unavailable"},
    }
    assert (
        lifecycle_status_for_snapshot(snapshot, previous_status="partially_received")
        == "partially_received"
    )


def _exception_order(db, stable_key="stabilization-test"):
    from app.models.procurement_order_formation import ProcurementOrderFormation

    order = ProcurementOrderFormation(
        stable_key=stable_key,
        supplier_name="Поставщик",
        contract_name="Договор",
        warehouse_name="Склад",
        batch_id="test",
        calculation_id="test",
        order_date=date(2026, 9, 1),
        origin="onec_import",
        lifecycle_status="active",
        onec_open_quantity=10,
        payload={"receipt_evidence": {"status": "exact"}},
    )
    db.add(order)
    db.flush()
    return order


def test_exception_recalculation_preserves_first_detection_and_deadline(db_session):
    from sqlalchemy import select

    from app.models.procurement_exception import ProcurementException
    from app.services.procurement_exceptions import sync_exceptions

    order = _exception_order(db_session)
    first = datetime(2026, 9, 4, 9)
    sync_exceptions(db_session, orders=[order], now=first)
    item = db_session.scalar(select(ProcurementException))
    due, version, digest = item.response_due_at, item.version, item.facts_hash
    sync_exceptions(db_session, orders=[order], now=datetime(2026, 9, 7, 12))
    assert item.first_seen_at == first
    assert item.response_due_at == due
    assert item.version == version
    order.onec_open_quantity = 12
    sync_exceptions(db_session, orders=[order], now=datetime(2026, 9, 8, 12))
    assert item.facts_hash != digest
    assert item.first_seen_at == first
    assert item.response_due_at == due
    assert item.version == version + 1


def test_exception_acknowledgement_requires_action_and_due(db_session):
    from sqlalchemy import select

    from app.models.procurement_exception import ProcurementException
    from app.services.procurement_exceptions import (
        decide_exception,
        serialize_exception,
        sync_exceptions,
    )

    order = _exception_order(db_session)
    now = datetime(2026, 9, 4, 9)
    sync_exceptions(db_session, orders=[order], now=now)
    item = db_session.scalar(select(ProcurementException))
    values = dict(expected_version=item.version, facts_hash=item.facts_hash, status="in_progress")
    with pytest.raises(ValueError, match="следующее действие"):
        decide_exception(db_session, item.id, values=values, user_id="7", actor="buyer:7", now=now)
    values.update(
        next_action="Получить дату от поставщика", next_action_due_at=datetime(2026, 9, 7, 10)
    )
    decide_exception(db_session, item.id, values=values, user_id="7", actor="buyer:7", now=now)
    assert item.assigned_user_id == "7"
    assert not serialize_exception(item, now=now)["overdue"]
    assert serialize_exception(item, now=datetime(2026, 9, 7, 11))["overdue"]
    acknowledged = item.acknowledged_at
    order.onec_open_quantity = 12
    sync_exceptions(db_session, orders=[order], now=now)
    assert item.status == "in_progress"
    assert item.acknowledged_at == acknowledged
    assert item.next_action == "Получить дату от поставщика"
    assert serialize_exception(item, now=datetime(2026, 9, 7, 11))["overdue"]


def test_exception_cannot_close_missing_date_by_comment(db_session):
    from sqlalchemy import select

    from app.models.procurement_exception import ProcurementException
    from app.services.procurement_exceptions import decide_exception, sync_exceptions

    order = _exception_order(db_session)
    now = datetime(2026, 9, 4, 9)
    sync_exceptions(db_session, orders=[order], now=now)
    item = db_session.scalar(select(ProcurementException))
    with pytest.raises(ValueError, match="повторным чтением"):
        decide_exception(
            db_session,
            item.id,
            values=dict(
                expected_version=item.version,
                facts_hash=item.facts_hash,
                status="resolved",
                reason="Просмотрено",
                evidence="Комментарий",
            ),
            user_id="7",
            actor="buyer:7",
            now=now,
        )
    order.expected_receipt_date = date(2026, 9, 10)
    sync_exceptions(db_session, orders=[order], now=now)
    assert item.status == "resolved"


def test_historical_removal_recovery_requires_explicit_evidence():
    from app.services.procurement_manual_removal_recovery import plan_manual_removal_recovery

    line = dict(id=1, order_id=2, version=3, payload={})
    event = dict(
        id=4,
        order_id=2,
        entity_id="1",
        entity_type="order_line",
        event_type="order_line_removed",
        actor="buyer:7",
        created_at="2026-08-20T09:00:00",
        payload={"removal_reason": "Поставка подтверждена"},
        before={"lines": [{"id": 1, "removed": False}]},
        after={"lines": [{"id": 1, "removed": True}]},
    )
    result = plan_manual_removal_recovery([line], [event])
    assert result[0]["status"] == "recoverable"
    assert result[0]["manual_removal"]["actor"] == "buyer:7"
    ambiguous = {**event, "payload": {}}
    assert (
        plan_manual_removal_recovery([line], [ambiguous])[0]["status"] == "requires_reconciliation"
    )
    restored = {
        **event,
        "id": 5,
        "created_at": "2026-08-21T09:00:00",
        "event_type": "order_line_restored",
        "after": {"lines": [{"id": 1, "removed": False}]},
    }
    assert plan_manual_removal_recovery([line], [event, restored]) == []


def test_stockout_exception_keeps_deadline_when_sku_moves_to_new_draft(db_session):
    from sqlalchemy import select

    from app.models.procurement_exception import ProcurementException
    from app.models.procurement_order_formation import ProcurementOrderFormationLine
    from app.services.procurement_exceptions import sync_exceptions

    def draft(key):
        order = _exception_order(db_session, key)
        order.origin = "generated"
        order.lines.append(
            ProcurementOrderFormationLine(
                stable_key=key + ":sku",
                line_number=1,
                bitrix_product_xml_id="abc",
                nomenclature_ref="0xabc",
                nomenclature_code="sku",
                nomenclature_name="Товар",
                payload={
                    "stockout_guard_triggered": True,
                    "stockout_guard_days_remaining": "2",
                    "stockout_guard_required_days": "20",
                },
            )
        )
        db_session.flush()
        return order

    first = draft("first")
    sync_exceptions(db_session, orders=[first], now=datetime(2026, 9, 4, 9))
    item = db_session.scalar(
        select(ProcurementException).where(ProcurementException.reason_code == "stockout_risk")
    )
    due, found, item_id = item.response_due_at, item.first_seen_at, item.id
    second = draft("second")
    first.status = "superseded"
    sync_exceptions(db_session, orders=[first, second], now=datetime(2026, 9, 7, 12))
    risks = db_session.scalars(
        select(ProcurementException).where(ProcurementException.reason_code == "stockout_risk")
    ).all()
    assert len(risks) == 1
    assert risks[0].id == item_id
    assert risks[0].order_id == second.id
    assert risks[0].response_due_at == due
    assert risks[0].first_seen_at == found
    assert risks[0].status == "new"


def test_receipt_query_validates_hex_literals_and_filters_indexed_skus(monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace

    from app.services import procurement_receipt_evidence as evidence

    snapshot = {
        "onec_ref": "0x" + "a" * 32,
        "lines": [
            {"item_ref_hex": "0x" + "b" * 32, "quantity": 10},
        ],
    }
    calls = []

    def execute(statement):
        query = str(statement)
        assert "movement._Fld7151RRef IN (0x" in query
        assert "movement._Fld7149RRef IN (0x" in query
        calls.append(query)
        return SimpleNamespace(
            mappings=lambda: [
                {
                    "order_ref": snapshot["onec_ref"],
                    "item_ref": "0x" + "b" * 32,
                    "receipt_ref": "0x" + "c" * 32,
                    "receipt_number": "Приход-1",
                    "receipt_at": datetime(2026, 9, 5),
                    "quantity": Decimal("4"),
                }
            ]
        )

    engine = SimpleNamespace(
        connect=lambda: nullcontext(SimpleNamespace(execute=execute)), dispose=lambda: None
    )
    monkeypatch.setattr(evidence, "build_onec_engine", lambda *args, **kwargs: engine)
    evidence.load_receipt_evidence("test", [snapshot])
    assert len(calls) == 1
    receipt_sql, return_sql, adjustment_sql = calls[0].split(" UNION ALL ")
    assert "_RecordKind = 1" in receipt_sql
    assert "_RecordKind = 0" in return_sql
    assert "_Document110" in return_sql
    assert "_RecordKind = 1" in adjustment_sql
    assert snapshot["received_qty"] == "4"
    assert snapshot["lines"][0]["received_quantity"] == "4"


def test_missing_sku_does_not_fabricate_zero_receipt():
    snapshot = {"lines": [{"quantity": 10}]}
    attach_receipt_evidence(snapshot, [])
    assert snapshot["receipt_evidence"]["status"] == "unconfirmed"
    assert snapshot["received_qty"] is None


@pytest.mark.parametrize("value", ["0xabc", "0x' OR 1=1 --", "0x" + "a" * 33])
def test_receipt_reference_filter_rejects_noncanonical_sql_input(value):
    from app.services.procurement_receipt_evidence import receipt_reference_list

    with pytest.raises(ValueError):
        receipt_reference_list([value])


def test_returns_and_obligation_corrections_never_inflate_receipts():
    snapshot = {
        "posted": True,
        "ordered_qty": 10,
        "open_qty": 0,
        "lines": [{"item_ref_hex": "sku", "quantity": 10}],
    }
    receipt = {"item_ref": "sku", "receipt_ref": "receipt", "quantity": "10"}
    returned = {"item_ref": "sku", "receipt_ref": "return", "quantity": "3"}
    correction = {"item_ref": "sku", "receipt_ref": "correction", "quantity": "2"}
    attach_receipt_evidence(snapshot, [receipt], returns=[returned], adjustments=[correction])
    evidence = snapshot["receipt_evidence"]
    assert evidence["received_quantity"] == "10"
    assert evidence["return_quantity"] == "3"
    assert evidence["adjustment_quantity"] == "-2"
    assert evidence["adjustment_movements"][0]["quantity"] == "-2"
    assert lifecycle_status_for_snapshot(snapshot) == "received"
    assert snapshot["lines"][0]["received_quantity"] == "10"


def test_order_closed_by_correction_without_receipt_requires_reconciliation():
    snapshot = {
        "posted": True,
        "ordered_qty": 10,
        "open_qty": 0,
        "lines": [{"item_ref_hex": "sku", "quantity": 10}],
    }
    attach_receipt_evidence(
        snapshot,
        [],
        adjustments=[{"item_ref": "sku", "receipt_ref": "adjustment", "quantity": "10"}],
    )
    assert snapshot["receipt_evidence"]["adjustment_quantity"] == "-10"
    assert snapshot["received_qty"] == "0"
    assert lifecycle_status_for_snapshot(snapshot) == "reconciliation_required"


def test_supply_confirmation_is_invalidated_by_a_later_quantity_edit(db_session):
    from sqlalchemy import select

    from app.models.procurement_exception import ProcurementException
    from app.models.procurement_order_formation import (
        ProcurementOrderFormationEvent,
        ProcurementOrderFormationLine,
    )
    from app.services.procurement_exceptions import decide_exception, sync_exceptions
    from app.services.procurement_order_formation import line_blockers, update_order_line

    order = _exception_order(db_session)
    order.origin = "generated"
    line = ProcurementOrderFormationLine(
        order=order,
        stable_key="supply-line",
        line_number=1,
        bitrix_product_xml_id="abc",
        nomenclature_ref="0xabc",
        nomenclature_code="sku",
        nomenclature_name="Товар",
        final_quantity=10,
        recommended_quantity=10,
        purchase_price=5,
        amount=50,
        payload={
            "price_confirmed": True,
            "supply_scenario": {
                "facts_hash": "a" * 64,
                "review_required": True,
                "all_open_quantity": "10",
                "dated_only_quantity": "20",
            },
        },
    )
    db_session.add(line)
    db_session.flush()
    sync_exceptions(db_session, orders=[order], now=datetime(2026, 9, 4, 9))
    item = db_session.scalar(
        select(ProcurementException).where(
            ProcurementException.reason_code == "supply_confirmation_required"
        )
    )
    due = item.response_due_at
    decide_exception(
        db_session,
        item.id,
        values={
            "expected_version": item.version,
            "facts_hash": item.facts_hash,
            "status": "resolved",
            "reason": "Нужна дополнительная закупка",
            "evidence": "Поставщик подтвердил отмену спорной поставки",
            "final_quantity": 20,
            "expected_order_version": order.version,
            "expected_line_version": line.version,
        },
        user_id="7",
        actor="buyer:7",
        now=datetime(2026, 9, 4, 10),
    )
    assert "supply_confirmation_required" not in line_blockers(line)
    event = db_session.scalar(
        select(ProcurementOrderFormationEvent).where(
            ProcurementOrderFormationEvent.event_type == "exception_decided"
        )
    )
    assert Decimal(event.before["final_quantity"]) == 10
    assert Decimal(event.after["final_quantity"]) == 20
    update_order_line(
        db_session,
        order.id,
        line.id,
        {
            "expected_order_version": order.version,
            "expected_line_version": line.version,
            "final_quantity": 25,
        },
        commit=False,
    )
    assert "supply_confirmation_required" in line_blockers(line)
    assert item.status == "new"
    assert item.response_due_at == due


def test_placeholder_price_is_never_confirmed_money():
    from types import SimpleNamespace

    from app.services.procurement_supply_scenarios import price_confirmed

    for source in ("onec_import", "generated"):
        assert not price_confirmed(
            SimpleNamespace(
                source_kind=source, purchase_price=Decimal(1), payload={"price_confirmed": True}
            )
        )


def test_quantity_decision_reason_is_saved_and_counted(db_session):
    from app.models.procurement_order_formation import ProcurementOrderFormationLine
    from app.services.procurement_exceptions import control_summary
    from app.services.procurement_order_formation import update_order_line

    order = _exception_order(db_session)
    order.origin = "generated"
    line = ProcurementOrderFormationLine(
        order=order,
        stable_key="reason-line",
        line_number=1,
        bitrix_product_xml_id="sku",
        nomenclature_ref="sku",
        nomenclature_code="sku",
        nomenclature_name="Товар",
        final_quantity=10,
        recommended_quantity=10,
        purchase_price=5,
        amount=50,
        payload={},
    )
    db_session.add(line)
    db_session.flush()
    update_order_line(
        db_session,
        order.id,
        line.id,
        {
            "expected_order_version": order.version,
            "expected_line_version": line.version,
            "final_quantity": 12,
            "quantity_reason": "Подтверждён заказ клиента",
            "_removal_actor": "buyer:7",
        },
        commit=False,
    )
    assert line.payload["quantity_decision"]["reason"] == "Подтверждён заказ клиента"
    result = control_summary(db_session)
    assert result["recommendation_change_reasons"] == {"Подтверждён заказ клиента": 1}
    assert result["recommendation_decisions"]["changed"] == 1
