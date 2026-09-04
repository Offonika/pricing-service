from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from app.core.config import Settings
from app.models import (
    LogisticsManualReview,
    LogisticsWarehouse,
    SiteOrderExecutionCase,
    SiteOrderExecutionEvent,
    SiteOrderStageOutbox,
)
from app.services import site_order_execution_reconciliation as execution_reconciliation
from app.services import site_order_stage_outbox as stage_outbox_service
from app.services.site_order_fulfillment import (
    CRM_ORDER_NUMBER_FIELD,
    BitrixDealSnapshot,
)
from app.services.site_order_stage_outbox import (
    PICKUP_ADDRESS_FIELD,
    PICKUP_POINT_FIELD,
    SMS_EVENT_FIELD,
    SMS_STATUS_FIELD,
    STORAGE_DEADLINE_FIELD,
    process_stage_outbox,
)
from tasks.check_logistics_stage_outbox_health import build_health_report


class FakeBitrixClient:
    def __init__(self, deal: BitrixDealSnapshot, *, fail_updates: int = 0) -> None:
        self.deal = deal
        self.fail_updates = fail_updates
        self.updates: list[dict] = []
        self.comments: list[str] = []

    def get_deal_by_id(self, deal_id: int):
        return self.deal if deal_id == self.deal.deal_id else None

    def list_deals_by_site_order(self, order_number: str):
        if self.deal.raw.get(CRM_ORDER_NUMBER_FIELD) == order_number:
            return [self.deal]
        return []

    def update_deal_fields(self, deal_id: int, fields: dict):
        if self.fail_updates:
            self.fail_updates -= 1
            raise RuntimeError("bitrix unavailable")
        assert deal_id == self.deal.deal_id
        self.updates.append(dict(fields))
        self.deal.raw = {**(self.deal.raw or {}), **fields}
        if "STAGE_ID" in fields:
            self.deal.stage_id = fields["STAGE_ID"]
        return True

    def update_deal_stage(self, deal_id: int, stage: str):
        return self.update_deal_fields(deal_id, {"STAGE_ID": stage})

    def get_contact_by_id(self, contact_id: int):
        assert contact_id == 77
        return {"ID": "77", "PHONE": [{"VALUE": "+79990000000"}]}

    def call(self, method: str, params: dict):
        if method == "crm.timeline.comment.list":
            return {"result": [{"COMMENT": comment} for comment in self.comments]}
        if method == "crm.timeline.comment.add":
            self.comments.append(params["fields"]["COMMENT"])
            return {"result": len(self.comments)}
        raise AssertionError(method)


def _seed_order_chain(db_session):
    warehouse = LogisticsWarehouse(
        external_id="central",
        name="Центральный склад",
        kind="central",
        payload={"address": "Москва, Тестовая, 1"},
    )
    db_session.add(warehouse)
    db_session.flush()
    case = SiteOrderExecutionCase(
        site_order_number="216951",
        bitrix_deal_id=9001,
        current_derived_status="pickup_stored_at_point",
    )
    db_session.add(case)
    db_session.flush()
    rows = []
    for index, (event_type, target_stage) in enumerate(
        [
            ("pickup_moving_to_point", "PICKUP_TRANSIT"),
            ("pickup_stored_at_point", "PICKUP_WAITING"),
        ],
        start=1,
    ):
        event = SiteOrderExecutionEvent(
            case_id=case.id,
            event_type=event_type,
            event_at=datetime(2026, 8, 26, 9, index),
            source="logistics",
            source_ref=f"event:{index}",
            confidence="strong",
            idempotency_key=f"event-{index}",
            payload={},
        )
        db_session.add(event)
        db_session.flush()
        row = SiteOrderStageOutbox(
            case_id=case.id,
            event_id=event.id,
            idempotency_key=f"stage-{index}",
            site_order_number=case.site_order_number,
            bitrix_deal_id=case.bitrix_deal_id,
            source_event_type=event_type,
            target_stage=target_stage,
            payload={
                "warehouse_id": warehouse.id,
                "warehouse_name": warehouse.name,
                "event_at": event.event_at.isoformat(),
                "source_channel": "bitrix",
            },
        )
        db_session.add(row)
        rows.append(row)
    db_session.commit()
    return rows


def _settings(**overrides):
    values = {
        "logistics_stage_automation_enabled": True,
        "logistics_stage_pilot_warehouse_external_ids": ["central"],
        "pickup_ready_sms_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)


def test_logistics_stage_outbox_health_reports_pilot_delay_and_reviews(db_session) -> None:
    rows = _seed_order_chain(db_session)
    rows[0].status = "retry"
    rows[0].created_at = datetime(2026, 8, 28, 9, 0, 0)
    rows[1].status = "manual_review"
    rows[1].created_at = datetime(2026, 8, 28, 9, 0, 5)
    db_session.commit()

    report = build_health_report(
        db_session,
        pilot_warehouse_external_ids=["central"],
        now=datetime(2026, 8, 28, 9, 0, 31),
        max_delay_seconds=30,
    )

    assert report["status"] == "critical"
    assert report["retry"] == 1
    assert report["manual_review"] == 1
    assert report["delayed"] == 1
    assert report["oldest_active_age_seconds"] == 31
    assert report["delayed_outbox_ids"] == [rows[0].id]


def _deal(
    stage: str = "FINAL_INVOICE",
    *,
    deal_id: int = 9001,
    site_order_number: str = "216951",
):
    return BitrixDealSnapshot(
        deal_id=deal_id,
        stage_id=stage,
        raw={
            "ID": str(deal_id),
            "STAGE_ID": stage,
            CRM_ORDER_NUMBER_FIELD: site_order_number,
            "CONTACT_ID": "77",
        },
    )


def _seed_execution_outbox(
    db_session,
    *,
    historical: bool = False,
    current_stage: str = "EXECUTING",
    site_order_number: str = "216951",
    deal_id: int = 9001,
):
    snapshot = execution_reconciliation.ExecutionEvidenceSnapshot(
        site_order_number=site_order_number,
        bitrix_deal_id=deal_id,
        current_stage=current_stage,
        delivery_class="pickup",
        raw_delivery="Самовывоз",
        duplicate_deal_ids=(deal_id,),
        rtu_count=1,
        assembled_rtu_count=1,
        line_coverage_status="complete",
        latest_rtu_at=datetime(2026, 8, 26, 9, 0),
        latest_assembled_at=datetime(2026, 8, 26, 9, 5),
        issued_rtu_count=1 if current_stage == "DISMANTLING" else 0,
        latest_issued_at=(datetime(2026, 8, 26, 10, 0) if current_stage == "DISMANTLING" else None),
        dismantling_started_at=(
            datetime(2026, 8, 26, 9, 30) if current_stage == "DISMANTLING" else None
        ),
        historical=historical,
    )
    result = execution_reconciliation.persist_execution_decision(
        db_session,
        snapshot=snapshot,
        decision=execution_reconciliation.decide_execution_stage(snapshot),
    )
    db_session.commit()
    assert result.outbox_id is not None
    return db_session.get(SiteOrderStageOutbox, result.outbox_id)


def test_execution_outbox_can_be_scoped_to_affected_orders(db_session) -> None:
    target = _seed_execution_outbox(db_session)
    untouched = _seed_execution_outbox(
        db_session,
        site_order_number="216952",
        deal_id=9002,
    )
    client = FakeBitrixClient(_deal("EXECUTING"))

    results = process_stage_outbox(
        db_session,
        client=client,
        apply=True,
        settings=_settings(
            order_fulfillment_execution_master_enabled=True,
            order_fulfillment_execution_stage_apply_enabled=True,
        ),
        site_order_numbers=["216951"],
    )

    assert [item.outbox_id for item in results] == [target.id]
    db_session.refresh(untouched)
    assert untouched.status == "pending"


def test_dismantling_execution_outbox_stays_shadow_until_separate_gate(db_session) -> None:
    row = _seed_execution_outbox(db_session, current_stage="DISMANTLING")
    client = FakeBitrixClient(_deal("DISMANTLING"))
    settings = _settings(
        order_fulfillment_execution_master_enabled=True,
        order_fulfillment_execution_stage_apply_enabled=True,
        order_fulfillment_dismantling_auto_apply_enabled=False,
    )

    shadow = process_stage_outbox(
        db_session,
        client=client,
        apply=True,
        settings=settings,
    )

    assert [item.result for item in shadow] == ["dismantling_auto_apply_disabled"]
    assert client.updates == []
    db_session.refresh(row)
    assert row.status == "pending"

    enabled = process_stage_outbox(
        db_session,
        client=client,
        apply=True,
        settings=_settings(
            order_fulfillment_execution_master_enabled=True,
            order_fulfillment_execution_stage_apply_enabled=True,
            order_fulfillment_dismantling_auto_apply_enabled=True,
        ),
    )
    assert [item.result for item in enabled] == ["applied"]
    assert client.deal.stage_id == "WON"


def test_stage_outbox_applies_chain_in_order_and_is_idempotent(db_session) -> None:
    rows = _seed_order_chain(db_session)
    client = FakeBitrixClient(_deal())

    results = process_stage_outbox(
        db_session,
        client=client,
        apply=True,
        settings=_settings(),
        now=datetime(2026, 8, 26, 10, 0),
    )

    assert [result.result for result in results] == ["applied", "applied"]
    stage_updates = [item["STAGE_ID"] for item in client.updates if "STAGE_ID" in item]
    assert stage_updates == ["PICKUP_TRANSIT", "PICKUP_WAITING"]
    assert client.deal.raw[SMS_EVENT_FIELD] == str(rows[1].event_id)
    assert client.deal.raw[SMS_STATUS_FIELD] == "shadow"
    assert (
        process_stage_outbox(
            db_session,
            client=client,
            apply=True,
            settings=_settings(),
        )
        == []
    )


def test_stage_outbox_selects_predecessor_before_newer_receipt(db_session) -> None:
    rows = _seed_order_chain(db_session)
    rows[0].created_at = datetime(2026, 8, 26, 9, 1)
    rows[1].created_at = datetime(2026, 8, 26, 9, 2)
    db_session.commit()
    client = FakeBitrixClient(_deal())

    first_batch = process_stage_outbox(
        db_session,
        client=client,
        apply=True,
        limit=1,
        settings=_settings(),
        now=datetime(2026, 8, 26, 10, 0),
    )

    assert [result.outbox_id for result in first_batch] == [rows[0].id]
    assert [result.result for result in first_batch] == ["applied"]
    assert client.deal.stage_id == "PICKUP_TRANSIT"


def test_stage_outbox_does_not_starve_logistics_predecessor_for_execution_row(
    db_session,
) -> None:
    rows = _seed_order_chain(db_session)
    case_row = db_session.get(SiteOrderExecutionCase, rows[0].case_id)
    execution_event = SiteOrderExecutionEvent(
        case_id=case_row.id,
        event_type="execution_assembled",
        event_at=datetime(2026, 8, 26, 9, 3),
        source="onec",
        source_ref="execution:later",
        confidence="strong",
        idempotency_key="execution-later",
        payload={},
    )
    db_session.add(execution_event)
    db_session.flush()
    case_row.last_evidence_event_id = execution_event.id
    execution_row = SiteOrderStageOutbox(
        case_id=case_row.id,
        event_id=execution_event.id,
        idempotency_key="stage-execution-later",
        site_order_number=case_row.site_order_number,
        bitrix_deal_id=case_row.bitrix_deal_id,
        source_event_type=execution_event.event_type,
        target_stage="FINAL_INVOICE",
        payload={},
    )
    db_session.add(execution_row)
    db_session.commit()
    client = FakeBitrixClient(_deal())
    settings = _settings(
        order_fulfillment_execution_master_enabled=True,
        order_fulfillment_execution_stage_apply_enabled=True,
    )

    results = [
        process_stage_outbox(
            db_session,
            client=client,
            apply=True,
            limit=1,
            settings=settings,
            now=datetime(2026, 8, 26, 10, 0),
        )[0]
        for _ in range(3)
    ]

    assert [result.outbox_id for result in results] == [
        rows[0].id,
        rows[1].id,
        execution_row.id,
    ]
    assert [result.result for result in results] == ["applied", "applied", "superseded"]
    assert db_session.scalars(
        select(SiteOrderStageOutbox.status).order_by(SiteOrderStageOutbox.id)
    ).all() == ["applied", "applied", "applied"]


def test_stage_outbox_does_not_prioritize_new_execution_from_another_order(
    db_session,
) -> None:
    rows = _seed_order_chain(db_session)
    another_case = SiteOrderExecutionCase(
        site_order_number="999999",
        bitrix_deal_id=9999,
        current_derived_status="execution_assembled",
    )
    db_session.add(another_case)
    db_session.flush()
    execution_event = SiteOrderExecutionEvent(
        case_id=another_case.id,
        event_type="execution_assembled",
        event_at=datetime(2026, 8, 26, 9, 3),
        source="onec",
        source_ref="execution:another-order",
        confidence="strong",
        idempotency_key="execution-another-order",
        payload={},
    )
    db_session.add(execution_event)
    db_session.flush()
    another_case.last_evidence_event_id = execution_event.id
    execution_row = SiteOrderStageOutbox(
        case_id=another_case.id,
        event_id=execution_event.id,
        idempotency_key="stage-execution-another-order",
        site_order_number=another_case.site_order_number,
        bitrix_deal_id=another_case.bitrix_deal_id,
        source_event_type=execution_event.event_type,
        target_stage="FINAL_INVOICE",
        payload={},
    )
    db_session.add(execution_row)
    db_session.commit()

    first_batch = process_stage_outbox(
        db_session,
        client=FakeBitrixClient(_deal()),
        apply=True,
        limit=1,
        settings=_settings(),
        now=datetime(2026, 8, 26, 10, 0),
    )

    assert [result.outbox_id for result in first_batch] == [rows[0].id]
    assert rows[0].id < execution_row.id


def test_stage_outbox_retries_without_overtaking_next_event(db_session) -> None:
    rows = _seed_order_chain(db_session)
    client = FakeBitrixClient(_deal(), fail_updates=1)
    first_now = datetime(2026, 8, 26, 10, 0)

    results = process_stage_outbox(
        db_session,
        client=client,
        apply=True,
        settings=_settings(),
        now=first_now,
    )

    assert [result.result for result in results] == ["retry", "waiting_for_predecessor"]
    db_session.refresh(rows[0])
    db_session.refresh(rows[1])
    assert rows[0].status == "retry"
    assert rows[1].status == "pending"

    recovered = process_stage_outbox(
        db_session,
        client=client,
        apply=True,
        settings=_settings(),
        now=first_now + timedelta(minutes=1),
    )
    assert [result.result for result in recovered] == ["applied", "applied"]


def test_stage_outbox_manual_review_blocks_next_event(db_session) -> None:
    rows = _seed_order_chain(db_session)
    rows[0].status = "manual_review"
    rows[0].last_error = "unexpected_live_stage:PREPARATION"
    db_session.commit()
    client = FakeBitrixClient(_deal())

    results = process_stage_outbox(
        db_session,
        client=client,
        apply=True,
        settings=_settings(),
        now=datetime(2026, 8, 26, 10, 0),
    )

    assert [result.result for result in results] == ["waiting_for_predecessor"]
    assert client.updates == []
    db_session.refresh(rows[1])
    assert rows[1].status == "pending"


def test_stage_outbox_never_changes_terminal_deal(db_session) -> None:
    rows = _seed_order_chain(db_session)
    client = FakeBitrixClient(_deal("WON"))

    results = process_stage_outbox(
        db_session,
        client=client,
        apply=True,
        settings=_settings(),
    )

    assert results[0].result == "terminal_live_stage"
    assert client.updates == []
    db_session.refresh(rows[0])
    assert rows[0].status == "terminal"
    assert db_session.scalars(select(SiteOrderStageOutbox)).all()


def test_stage_outbox_moves_unexpected_live_stage_to_review(db_session) -> None:
    rows = _seed_order_chain(db_session)
    client = FakeBitrixClient(_deal("EXECUTING"))

    results = process_stage_outbox(
        db_session,
        client=client,
        apply=True,
        settings=_settings(),
    )

    assert results[0].result == "manual_review"
    assert client.deal.stage_id == "DELIVERY_REVIEW"
    assert any("DELIVERY_REVIEW" in comment for comment in client.comments)
    db_session.refresh(rows[0])
    assert rows[0].status == "manual_review"
    assert db_session.scalar(select(LogisticsManualReview)) is not None


def test_stage_outbox_marks_sms_ready_only_for_valid_enabled_pilot(db_session) -> None:
    rows = _seed_order_chain(db_session)
    client = FakeBitrixClient(_deal())

    results = process_stage_outbox(
        db_session,
        client=client,
        apply=True,
        settings=_settings(
            pickup_ready_sms_enabled=True,
            pickup_ready_sms_pilot_warehouse_external_ids=["central"],
        ),
        now=datetime(2026, 8, 26, 10, 0),
    )

    assert [result.result for result in results] == ["applied", "applied"]
    assert client.deal.raw[SMS_EVENT_FIELD] == str(rows[1].event_id)
    assert client.deal.raw[SMS_STATUS_FIELD] == "ready"
    assert client.deal.raw[PICKUP_POINT_FIELD] == "Центральный склад"
    assert client.deal.raw[PICKUP_ADDRESS_FIELD] == "Москва, Тестовая, 1"
    assert client.deal.raw[STORAGE_DEADLINE_FIELD] == "2026-08-29T09:02:00"


def test_stage_outbox_rejects_live_deal_with_another_order_number(db_session) -> None:
    rows = _seed_order_chain(db_session)
    deal = _deal()
    deal.raw[CRM_ORDER_NUMBER_FIELD] = "another-order"
    client = FakeBitrixClient(deal)

    results = process_stage_outbox(
        db_session,
        client=client,
        apply=True,
        settings=_settings(),
    )

    assert results[0].result == "manual_review"
    assert results[0].reason == "bitrix_order_mismatch:another-order"
    assert client.updates == []
    db_session.refresh(rows[0])
    assert rows[0].status == "manual_review"


def test_execution_outbox_applies_without_logistics_pilot_gate(db_session) -> None:
    row = _seed_execution_outbox(db_session)
    client = FakeBitrixClient(_deal("EXECUTING"))

    results = process_stage_outbox(
        db_session,
        client=client,
        apply=True,
        settings=_settings(
            logistics_stage_automation_enabled=False,
            logistics_stage_pilot_warehouse_external_ids=[],
            order_fulfillment_execution_master_enabled=True,
            order_fulfillment_execution_stage_apply_enabled=True,
        ),
    )

    assert [item.result for item in results] == ["applied"]
    assert client.deal.stage_id == "FINAL_INVOICE"
    db_session.refresh(row)
    assert row.status == "applied"


def test_full_assembly_field_requires_exact_readback_before_stage(db_session) -> None:
    row = _seed_execution_outbox(db_session)
    client = FakeBitrixClient(_deal("EXECUTING"))
    original_update = client.update_deal_fields

    def ignore_full_assembly(deal_id: int, fields: dict):
        if stage_outbox_service.FULL_ASSEMBLY_FIELD in fields:
            client.updates.append(dict(fields))
            return True
        return original_update(deal_id, fields)

    client.update_deal_fields = ignore_full_assembly

    results = process_stage_outbox(
        db_session,
        client=client,
        apply=True,
        settings=_settings(
            order_fulfillment_execution_master_enabled=True,
            order_fulfillment_execution_stage_apply_enabled=True,
        ),
    )

    assert [item.result for item in results] == ["retry"]
    assert results[0].reason == "full_assembly_field_readback_mismatch"
    assert client.deal.stage_id == "EXECUTING"
    db_session.refresh(row)
    assert row.status == "retry"


def test_execution_outbox_historical_apply_is_independently_disabled(db_session) -> None:
    row = _seed_execution_outbox(db_session, historical=True)
    client = FakeBitrixClient(_deal("EXECUTING"))

    results = process_stage_outbox(
        db_session,
        client=client,
        apply=True,
        settings=_settings(
            order_fulfillment_execution_master_enabled=True,
            order_fulfillment_execution_stage_apply_enabled=True,
            order_fulfillment_execution_historical_apply_enabled=False,
        ),
    )

    assert [item.result for item in results] == ["historical_apply_disabled"]
    assert client.updates == []
    db_session.refresh(row)
    assert row.status == "pending"


def test_execution_outbox_duplicate_deal_is_manual_without_stage_change(db_session) -> None:
    row = _seed_execution_outbox(db_session)
    client = FakeBitrixClient(_deal("EXECUTING"))
    duplicate = _deal("EXECUTING")
    duplicate.deal_id = 9002
    client.list_deals_by_site_order = lambda order_number: [client.deal, duplicate]

    results = process_stage_outbox(
        db_session,
        client=client,
        apply=True,
        settings=_settings(
            order_fulfillment_execution_master_enabled=True,
            order_fulfillment_execution_stage_apply_enabled=True,
        ),
    )

    assert [item.result for item in results] == ["manual_review"]
    assert results[0].reason == "multiple_bitrix_deals"
    assert client.deal.stage_id == "EXECUTING"
    assert client.updates == []
    db_session.refresh(row)
    assert row.status == "manual_review"


def test_execution_historical_apply_is_capped_at_twenty_per_run(
    db_session,
    monkeypatch,
) -> None:
    for index in range(25):
        case = SiteOrderExecutionCase(
            site_order_number=str(240000 + index),
            bitrix_deal_id=9100 + index,
            current_derived_status="execution_historical_assembled",
        )
        db_session.add(case)
        db_session.flush()
        event = SiteOrderExecutionEvent(
            case_id=case.id,
            event_type="execution_historical_assembled",
            event_at=datetime(2026, 8, 1, 10, 0),
            source="onec",
            source_ref=f"historical:{index}",
            confidence="strong",
            idempotency_key=f"historical-event:{index}",
            payload={},
        )
        db_session.add(event)
        db_session.flush()
        case.last_evidence_event_id = event.id
        db_session.add(
            SiteOrderStageOutbox(
                case_id=case.id,
                event_id=event.id,
                idempotency_key=f"historical-outbox:{index}",
                site_order_number=case.site_order_number,
                bitrix_deal_id=case.bitrix_deal_id,
                source_event_type="execution_historical_assembled",
                target_stage="FINAL_INVOICE",
                payload={"pipeline": "execution_reconciliation", "historical": True},
            )
        )
    db_session.commit()
    applied_ids: list[int] = []

    def fake_process_row(session, row, **kwargs):
        del session, kwargs
        applied_ids.append(row.id)
        return stage_outbox_service._result(row, "applied", applied=True)

    monkeypatch.setattr(stage_outbox_service, "_process_row", fake_process_row)

    results = process_stage_outbox(
        db_session,
        client=FakeBitrixClient(_deal("EXECUTING")),
        apply=True,
        limit=25,
        settings=_settings(
            order_fulfillment_execution_master_enabled=True,
            order_fulfillment_execution_stage_apply_enabled=True,
            order_fulfillment_execution_historical_apply_enabled=True,
        ),
    )

    assert len(applied_ids) == 20
    assert [item.result for item in results].count("applied") == 20
    assert [item.result for item in results].count("historical_batch_limit") == 5


def _seed_shipment_stage_row(
    db_session,
    *,
    case: SiteOrderExecutionCase | None = None,
    target_stage: str,
    suffix: str,
):
    if case is None:
        case = SiteOrderExecutionCase(
            site_order_number="216951",
            bitrix_deal_id=9001,
            current_derived_status="shipment_reconciliation",
        )
        db_session.add(case)
        db_session.flush()
    event = SiteOrderExecutionEvent(
        case_id=case.id,
        event_type=f"shipment_{suffix}",
        event_at=datetime(2026, 8, 29, 12, 0),
        source="onec",
        source_ref=f"shipment:{suffix}",
        confidence="strong",
        idempotency_key=f"shipment-event:{suffix}",
        payload={},
    )
    db_session.add(event)
    db_session.flush()
    case.last_evidence_event_id = event.id
    row = SiteOrderStageOutbox(
        case_id=case.id,
        event_id=event.id,
        idempotency_key=f"shipment-stage:{suffix}",
        site_order_number=case.site_order_number,
        bitrix_deal_id=case.bitrix_deal_id,
        source_event_type=event.event_type,
        target_stage=target_stage,
        payload={
            "pipeline": "shipment_reconciliation",
            "coverage_status": "complete",
            "event_at": event.event_at.isoformat(),
        },
    )
    db_session.add(row)
    db_session.commit()
    return case, row


def _seed_site_crm_signal_row(
    db_session,
    *,
    target_stage: str,
    event_type: str,
):
    case = SiteOrderExecutionCase(
        site_order_number="245388",
        bitrix_deal_id=39002,
        current_derived_status=event_type,
    )
    db_session.add(case)
    db_session.flush()
    event = SiteOrderExecutionEvent(
        case_id=case.id,
        event_type=event_type,
        event_at=datetime(2026, 9, 4, 13, 30),
        source="site_crm",
        source_ref=f"site:{event_type}",
        confidence="strong",
        idempotency_key=f"site-event:{event_type}",
        payload={"pipeline": "site_crm_signal"},
    )
    db_session.add(event)
    db_session.flush()
    case.last_evidence_event_id = event.id
    row = SiteOrderStageOutbox(
        case_id=case.id,
        event_id=event.id,
        idempotency_key=f"site-stage:{event_type}",
        site_order_number=case.site_order_number,
        bitrix_deal_id=case.bitrix_deal_id,
        source_event_type=event_type,
        target_stage=target_stage,
        payload={"pipeline": "site_crm_signal", "event_at": event.event_at.isoformat()},
    )
    db_session.add(row)
    db_session.commit()
    return row


def test_site_crm_delivery_signal_applies_only_with_separate_gate(db_session) -> None:
    _seed_site_crm_signal_row(
        db_session,
        target_stage="IN_DELIVERY",
        event_type="site_carrier_in_delivery",
    )
    client = FakeBitrixClient(_deal("FINAL_INVOICE", deal_id=39002, site_order_number="245388"))

    disabled = process_stage_outbox(
        db_session,
        client=client,
        apply=True,
        settings=_settings(order_fulfillment_execution_master_enabled=True),
    )
    assert disabled[0].result == "site_signal_automation_disabled"
    assert client.updates == []

    enabled = process_stage_outbox(
        db_session,
        client=client,
        apply=True,
        settings=_settings(
            order_fulfillment_execution_master_enabled=True,
            order_fulfillment_site_signal_stage_apply_enabled=True,
        ),
    )
    assert enabled[0].result == "applied"
    assert client.deal.stage_id == "IN_DELIVERY"


def test_site_crm_signal_cannot_move_protected_dismantling(db_session) -> None:
    _seed_site_crm_signal_row(
        db_session,
        target_stage="WON",
        event_type="site_carrier_delivered",
    )
    client = FakeBitrixClient(_deal("DISMANTLING", deal_id=39002, site_order_number="245388"))

    result = process_stage_outbox(
        db_session,
        client=client,
        apply=True,
        settings=_settings(
            order_fulfillment_execution_master_enabled=True,
            order_fulfillment_site_signal_stage_apply_enabled=True,
        ),
    )

    assert result[0].result == "manual_review"
    assert client.updates == []
    assert client.deal.stage_id == "DISMANTLING"


def test_shipment_stage_outbox_applies_partial_then_full_dispatch(db_session) -> None:
    case, partial_row = _seed_shipment_stage_row(
        db_session,
        target_stage="PARTIALLY_SHIPPED",
        suffix="partial",
    )
    client = FakeBitrixClient(_deal("FINAL_INVOICE"))
    settings = _settings(
        order_fulfillment_bot_apply_enabled=True,
        order_fulfillment_shipments_master_enabled=True,
        order_fulfillment_shipments_stage_apply_enabled=True,
    )

    partial = process_stage_outbox(
        db_session,
        client=client,
        apply=True,
        settings=settings,
    )

    assert [item.result for item in partial] == ["applied"]
    assert client.deal.stage_id == "PARTIALLY_SHIPPED"
    db_session.refresh(partial_row)
    assert partial_row.status == "applied"

    _, delivered_row = _seed_shipment_stage_row(
        db_session,
        case=case,
        target_stage="IN_DELIVERY",
        suffix="complete",
    )
    delivered = process_stage_outbox(
        db_session,
        client=client,
        apply=True,
        settings=settings,
    )

    assert [item.result for item in delivered] == ["applied"]
    assert client.deal.stage_id == "IN_DELIVERY"
    db_session.refresh(delivered_row)
    assert delivered_row.status == "applied"


def test_shipment_stage_outbox_is_fail_closed_when_disabled(db_session) -> None:
    _, row = _seed_shipment_stage_row(
        db_session,
        target_stage="PARTIALLY_SHIPPED",
        suffix="disabled",
    )
    client = FakeBitrixClient(_deal("FINAL_INVOICE"))

    results = process_stage_outbox(
        db_session,
        client=client,
        apply=True,
        settings=_settings(
            order_fulfillment_shipments_master_enabled=False,
            order_fulfillment_shipments_stage_apply_enabled=False,
        ),
    )

    assert [item.result for item in results] == ["shipment_automation_disabled"]
    assert client.deal.stage_id == "FINAL_INVOICE"
    assert client.updates == []
    db_session.refresh(row)
    assert row.status == "pending"


def test_shipment_stage_outbox_master_switch_blocks_stage(db_session) -> None:
    _, row = _seed_shipment_stage_row(
        db_session,
        target_stage="PARTIALLY_SHIPPED",
        suffix="master-disabled",
    )
    client = FakeBitrixClient(_deal("FINAL_INVOICE"))

    results = process_stage_outbox(
        db_session,
        client=client,
        apply=True,
        settings=_settings(
            order_fulfillment_bot_apply_enabled=False,
            order_fulfillment_shipments_master_enabled=True,
            order_fulfillment_shipments_stage_apply_enabled=True,
        ),
    )

    assert [item.result for item in results] == ["shipment_automation_disabled"]
    assert client.updates == []
    db_session.refresh(row)
    assert row.status == "pending"


def test_latest_strict_shipment_snapshot_supersedes_old_manual_review(db_session) -> None:
    case, old_row = _seed_shipment_stage_row(
        db_session,
        target_stage="PARTIALLY_SHIPPED",
        suffix="old-conflict",
    )
    old_row.status = "manual_review"
    old_row.last_error = "unexpected_live_stage:EXECUTING"
    db_session.commit()
    _, latest_row = _seed_shipment_stage_row(
        db_session,
        case=case,
        target_stage="IN_DELIVERY",
        suffix="strict-complete",
    )
    client = FakeBitrixClient(_deal("EXECUTING"))

    results = process_stage_outbox(
        db_session,
        client=client,
        apply=True,
        settings=_settings(
            order_fulfillment_bot_apply_enabled=True,
            order_fulfillment_shipments_master_enabled=True,
            order_fulfillment_shipments_stage_apply_enabled=True,
        ),
    )

    assert [item.result for item in results] == ["applied"]
    assert client.deal.stage_id == "IN_DELIVERY"
    assert client.deal.stage_id != "DELIVERY_REVIEW"
    db_session.refresh(old_row)
    db_session.refresh(latest_row)
    assert old_row.status == "applied"
    assert old_row.last_error == "superseded_by_newer_evidence"
    assert latest_row.status == "applied"
