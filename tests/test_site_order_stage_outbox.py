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


class FakeBitrixClient:
    def __init__(self, deal: BitrixDealSnapshot, *, fail_updates: int = 0) -> None:
        self.deal = deal
        self.fail_updates = fail_updates
        self.updates: list[dict] = []
        self.comments: list[str] = []

    def get_deal_by_id(self, deal_id: int):
        return self.deal if deal_id == self.deal.deal_id else None

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


def _deal(stage: str = "FINAL_INVOICE"):
    return BitrixDealSnapshot(
        deal_id=9001,
        stage_id=stage,
        raw={
            "ID": "9001",
            "STAGE_ID": stage,
            CRM_ORDER_NUMBER_FIELD: "216951",
            "CONTACT_ID": "77",
        },
    )


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
