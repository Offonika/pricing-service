from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.core.config import Settings
from app.models import (
    SiteOrderFulfillmentOutbox,
    SiteOrderRtu,
    SiteOrderShipment,
    SiteOrderShipmentNotification,
    SiteOrderStageOutbox,
)
from app.services import site_order_fulfillment as fulfillment
from app.services import site_order_fulfillment_bot as bot
from app.services import site_order_shipments as shipments


class _GatewayResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _GatewaySession:
    def __init__(self, responses: list[_GatewayResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.trust_env = True

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self.responses.pop(0)


class _BitrixClient:
    def __init__(
        self, order_number: str, deal_id: int, *, stage_id: str = "PARTIALLY_SHIPPED"
    ) -> None:
        self.order_number = order_number
        self.deal_id = deal_id
        self.stage_id = stage_id
        self.raw = {fulfillment.CRM_ORDER_NUMBER_FIELD: order_number}
        self.workflows: list[dict] = []
        self.timeline_comments: list[str] = []

    def get_deal_by_id(self, deal_id: int):
        if deal_id != self.deal_id:
            return None
        return fulfillment.BitrixDealSnapshot(
            deal_id=deal_id,
            stage_id=self.stage_id,
            raw=dict(self.raw),
        )

    def update_deal_fields(self, deal_id: int, fields: dict):
        assert deal_id == self.deal_id
        self.raw.update(fields)
        return True

    def start_business_process(self, **payload):
        self.workflows.append(payload)
        return "workflow-1"

    def call(self, method: str, params: dict | None = None):
        assert params is not None
        if method == "crm.timeline.comment.list":
            return {"result": [{"COMMENT": value} for value in self.timeline_comments]}
        if method == "crm.timeline.comment.add":
            self.timeline_comments.append(params["fields"]["COMMENT"])
            return {"result": len(self.timeline_comments)}
        raise AssertionError(method)


def _line(product_ref: str, quantity: str, **extra):
    return {"product_ref": product_ref, "quantity": quantity, **extra}


def _rtu(external_id: str, items: list[dict], *, assembled: bool = True):
    return {
        "external_id": external_id,
        "number": external_id,
        "posted": True,
        "assembled_at": datetime(2026, 8, 29, 10, 0) if assembled else None,
        "cancelled_at": None,
        "items": items,
    }


def _shipment(key: str, items: list[dict], *, status: str, tracking: str | None = None):
    return {
        "shipment_key": key,
        "bitrix_shipment_id": int(key.split("-")[-1]),
        "carrier": "cdek",
        "tracking_number": tracking,
        "status": status,
        "dispatched_at": (
            datetime(2026, 8, 29, 12, 0) if status == shipments.STATUS_DISPATCHED else None
        ),
        "items": items,
    }


def test_shipment_gateway_uses_bearer_and_exact_shipment_id() -> None:
    session = _GatewaySession(
        [
            _GatewayResponse({"ok": True, "shipments": [{"shipment_id": 41}]}),
            _GatewayResponse({"ok": True, "shipment": {"shipment_id": 42}}),
        ]
    )
    client = shipments.BitrixSaleShipmentGatewayClient(
        base_url="https://crm.example.invalid/local/tools/mm_sale_shipment_gateway.php",
        token="local-secret",
        session=session,
    )

    assert client.list_shipments(order_id=777) == [{"shipment_id": 41}]
    client.update_tracking(
        shipment_id=42,
        tracking_number="track-part-2",
        expected_revision=13,
    )

    assert session.trust_env is False
    assert session.calls[0]["headers"]["Authorization"] == "Bearer local-secret"
    assert session.calls[0]["headers"]["X-MM-Shipment-Token"] == "local-secret"
    assert session.calls[0]["json"] == {"action": "list", "order_id": 777}
    assert session.calls[1]["json"] == {
        "action": "update_tracking",
        "shipment_id": 42,
        "tracking_number": "track-part-2",
        "expected_revision": 13,
    }


def test_shipment_gateway_fails_closed_on_error_response() -> None:
    session = _GatewaySession(
        [_GatewayResponse({"ok": False, "error": "shipment_revision_conflict"}, 409)]
    )
    client = shipments.BitrixSaleShipmentGatewayClient(
        base_url="https://crm.example.invalid/local/tools/mm_sale_shipment_gateway.php",
        token="local-secret",
        session=session,
    )

    try:
        client.update_tracking(shipment_id=42, tracking_number="new-track")
    except shipments.ShipmentGatewayError as exc:
        assert str(exc) == "shipment_gateway_error:shipment_revision_conflict"
    else:
        raise AssertionError("shipment gateway error was not raised")


def test_coverage_uses_order_quantities_not_number_of_existing_rtus() -> None:
    expected = [_line("phone", "1"), _line("case", "1")]
    only_existing_rtu = [_line("phone", "1")]

    result = shipments.evaluate_assembly_coverage(expected, only_existing_rtu)

    assert result.status == "partial"
    assert result.complete is False
    assert result.missing_by_product == {"case": Decimal("1.0000")}


def test_two_rtus_can_form_one_physical_shipment() -> None:
    expected = [_line("phone", "1"), _line("case", "2")]
    assembled = [_line("phone", "1"), _line("case", "1"), _line("case", "1")]
    coverage = shipments.evaluate_assembly_coverage(expected, assembled)

    decision = shipments.derive_shipment_stage(
        current_stage="EXECUTING",
        coverage=coverage,
        expected_lines=expected,
        shipments=[
            _shipment(
                "shipment-436421",
                [_line("phone", "1"), _line("case", "2")],
                status=shipments.STATUS_READY,
                tracking="80223624331510",
            )
        ],
    )

    assert coverage.complete is True
    assert decision.target_stage == "FINAL_INVOICE"


def test_two_shipments_in_different_days_derive_partial_then_delivery() -> None:
    expected = [_line("phone", "1"), _line("case", "1")]
    coverage = shipments.evaluate_assembly_coverage(expected, expected)
    first = _shipment(
        "shipment-1",
        [_line("phone", "1")],
        status=shipments.STATUS_DISPATCHED,
        tracking="track-1",
    )
    second_planned = _shipment(
        "shipment-2", [_line("case", "1")], status=shipments.STATUS_READY, tracking="track-2"
    )
    second_dispatched = {**second_planned, "status": shipments.STATUS_DISPATCHED}

    partial = shipments.derive_shipment_stage(
        current_stage="FINAL_INVOICE",
        coverage=coverage,
        expected_lines=expected,
        shipments=[first, second_planned],
        delivery_kind=shipments.DELIVERY_CARRIER,
    )
    complete = shipments.derive_shipment_stage(
        current_stage="PARTIALLY_SHIPPED",
        coverage=coverage,
        expected_lines=expected,
        shipments=[first, second_dispatched],
        delivery_kind=shipments.DELIVERY_CARRIER,
    )

    assert partial.target_stage == "PARTIALLY_SHIPPED"
    assert complete.target_stage == "IN_DELIVERY"


def test_excess_assembly_or_returned_part_fails_closed() -> None:
    excess = shipments.evaluate_assembly_coverage([_line("phone", "1")], [_line("phone", "2")])
    assert excess.status == "conflict"
    assert (
        shipments.derive_shipment_stage(
            current_stage="EXECUTING",
            coverage=excess,
            expected_lines=[_line("phone", "1")],
            shipments=[],
        ).action
        == "manual_review"
    )

    complete = shipments.evaluate_assembly_coverage([_line("phone", "1")], [_line("phone", "1")])
    returned = shipments.derive_shipment_stage(
        current_stage="PARTIALLY_SHIPPED",
        coverage=complete,
        expected_lines=[_line("phone", "1")],
        shipments=[
            _shipment("shipment-1", [_line("phone", "1")], status=shipments.STATUS_RETURNED)
        ],
    )
    assert returned.action == "manual_review"


def test_dispatched_quantity_cannot_exceed_assembled_quantity() -> None:
    coverage = shipments.evaluate_assembly_coverage(
        [_line("phone", "1"), _line("case", "1")],
        [_line("phone", "1")],
    )

    decision = shipments.derive_shipment_stage(
        current_stage="EXECUTING",
        coverage=coverage,
        expected_lines=[_line("phone", "1"), _line("case", "1")],
        shipments=[
            _shipment(
                "shipment-1",
                [_line("phone", "1"), _line("case", "1")],
                status=shipments.STATUS_DISPATCHED,
                tracking="track-1",
            )
        ],
    )

    assert decision.action == "manual_review"
    assert decision.reason == "shipment_quantity_exceeds_assembled"


def test_duplicate_physical_allocation_or_tracking_fails_closed() -> None:
    expected = [_line("phone", "1")]
    coverage = shipments.evaluate_assembly_coverage(expected, expected)
    duplicated_allocation = shipments.derive_shipment_stage(
        current_stage="FINAL_INVOICE",
        coverage=coverage,
        expected_lines=expected,
        shipments=[
            _shipment("shipment-1", expected, status=shipments.STATUS_READY),
            _shipment("shipment-2", expected, status=shipments.STATUS_READY),
        ],
    )
    duplicated_tracking = shipments.derive_shipment_stage(
        current_stage="FINAL_INVOICE",
        coverage=coverage,
        expected_lines=expected,
        shipments=[
            _shipment(
                "shipment-1",
                [_line("phone", "0.5")],
                status=shipments.STATUS_READY,
                tracking="same-track",
            ),
            _shipment(
                "shipment-2",
                [_line("phone", "0.5")],
                status=shipments.STATUS_READY,
                tracking="same-track",
            ),
        ],
    )

    assert duplicated_allocation.reason == "shipment_allocation_excess"
    assert duplicated_tracking.reason == "shipment_tracking_duplicate"


def test_persisted_multi_shipment_snapshot_is_idempotent(db_session) -> None:
    expected = [_line("phone", "1"), _line("case", "1")]
    rtus = [
        _rtu("rtu-1", [_line("phone", "1")]),
        _rtu("rtu-2", [_line("case", "1")]),
    ]
    physical_shipments = [
        _shipment(
            "shipment-1",
            [_line("phone", "1", rtu_external_id="rtu-1")],
            status=shipments.STATUS_DISPATCHED,
            tracking="track-1",
        ),
        _shipment(
            "shipment-2",
            [_line("case", "1", rtu_external_id="rtu-2")],
            status=shipments.STATUS_READY,
            tracking="track-2",
        ),
    ]
    kwargs = {
        "site_order_number": "242685",
        "bitrix_deal_id": 39001,
        "current_stage": "FINAL_INVOICE",
        "expected_items": expected,
        "rtus": rtus,
        "shipments": physical_shipments,
        "event_at": datetime(2026, 8, 29, 12, 0),
        "persist": True,
        "enqueue_crm_fields": True,
        "enqueue_notifications": True,
        "email_enabled": True,
        "sms_enabled": True,
    }

    first = shipments.sync_order_shipments(db_session, **kwargs)
    db_session.commit()
    second = shipments.sync_order_shipments(db_session, **kwargs)
    db_session.commit()
    delivered_snapshot = [
        {**physical_shipments[0], "status": shipments.STATUS_DELIVERED},
        physical_shipments[1],
    ]
    third = shipments.sync_order_shipments(
        db_session,
        **{
            **kwargs,
            "current_stage": "PARTIALLY_SHIPPED",
            "shipments": delivered_snapshot,
        },
    )
    db_session.commit()

    assert first.full_assembly is True
    assert first.target_stage == "PARTIALLY_SHIPPED"
    assert first.notification_count == 2
    assert second.event_id is None
    assert second.notification_count == 0
    assert third.notification_count == 0
    assert db_session.scalar(select(func.count(SiteOrderShipment.id))) == 2
    assert db_session.scalar(select(func.count(SiteOrderShipmentNotification.id))) == 2
    assert db_session.scalar(select(func.count(SiteOrderStageOutbox.id))) == 1
    assert db_session.scalar(select(func.count(SiteOrderFulfillmentOutbox.id))) == 3


def test_single_shipment_keeps_legacy_notification_robot(db_session) -> None:
    result = shipments.sync_order_shipments(
        db_session,
        site_order_number="242686",
        bitrix_deal_id=39002,
        current_stage="EXECUTING",
        expected_items=[_line("phone", "1")],
        rtus=[_rtu("rtu-1", [_line("phone", "1")])],
        shipments=[
            _shipment(
                "shipment-1",
                [_line("phone", "1")],
                status=shipments.STATUS_READY,
                tracking="track-1",
            )
        ],
        event_at=datetime(2026, 8, 29, 12, 0),
        persist=True,
        enqueue_crm_fields=True,
        enqueue_notifications=True,
        email_enabled=True,
        sms_enabled=True,
    )
    db_session.commit()

    assert result.target_stage == "FINAL_INVOICE"
    assert result.notification_count == 0
    assert db_session.scalar(select(func.count(SiteOrderShipmentNotification.id))) == 0


def test_shipment_outbox_updates_guard_fields_and_starts_one_workflow(db_session) -> None:
    expected = [_line("phone", "1"), _line("case", "1")]
    shipments.sync_order_shipments(
        db_session,
        site_order_number="242687",
        bitrix_deal_id=39003,
        current_stage="FINAL_INVOICE",
        expected_items=expected,
        rtus=[_rtu("rtu-1", expected)],
        shipments=[
            _shipment(
                "shipment-1",
                [_line("phone", "1", rtu_external_id="rtu-1")],
                status=shipments.STATUS_DISPATCHED,
                tracking="track-1",
            ),
            _shipment(
                "shipment-2",
                [_line("case", "1", rtu_external_id="rtu-1")],
                status=shipments.STATUS_READY,
                tracking="track-2",
            ),
        ],
        event_at=datetime(2026, 8, 29, 12, 0),
        persist=True,
        enqueue_crm_fields=True,
        enqueue_notifications=True,
        email_enabled=True,
        sms_enabled=False,
    )
    db_session.commit()
    rows = db_session.scalars(
        select(SiteOrderFulfillmentOutbox).order_by(SiteOrderFulfillmentOutbox.id)
    ).all()
    client = _BitrixClient("242687", 39003)
    settings = Settings(
        _env_file=None,
        order_fulfillment_bot_apply_enabled=True,
        order_fulfillment_shipments_master_enabled=True,
        order_fulfillment_shipments_crm_fields_enabled=True,
        order_fulfillment_shipments_notifications_enabled=True,
        order_fulfillment_shipments_email_enabled=True,
        order_fulfillment_shipments_email_workflow_template_id=77,
    )

    for row in rows:
        bot._dispatch_outbox(  # noqa: SLF001
            db_session,
            row=row,
            client=client,
            settings=settings,
            onec_validator=lambda _: None,
            now=datetime(2026, 8, 29, 12, 5),
        )
    db_session.commit()

    assert client.raw[shipments.FULL_ASSEMBLY_FIELD]
    assert client.raw[shipments.SHIPMENT_COUNT_FIELD] == 2
    assert len(client.workflows) == 1
    assert client.workflows[0]["template_id"] == 77
    notification = db_session.scalar(select(SiteOrderShipmentNotification))
    assert notification is not None
    assert notification.status == "submitted"
    assert notification.submitted_at == datetime(2026, 8, 29, 12, 5)
    assert notification.sent_at is None


def test_snapshot_id_is_content_based_and_ignores_source_revision() -> None:
    expected = [_line("case", "1"), _line("phone", "1")]
    rtus = [
        _rtu("rtu-2", [_line("case", "1")]),
        _rtu("rtu-1", [_line("phone", "1")]),
    ]
    physical = [
        _shipment(
            "shipment-1",
            [_line("phone", "1", rtu_external_id="rtu-1")],
            status=shipments.STATUS_READY,
        )
    ]

    first = shipments.build_snapshot_id(
        site_order_number="242700",
        delivery_kind=shipments.DELIVERY_CARRIER,
        expected_items=expected,
        rtus=rtus,
        shipments=physical,
        source_revisions={"onec": "revision-1", "bitrix_sale": "revision-1"},
    )
    same_content = shipments.build_snapshot_id(
        site_order_number="242700",
        delivery_kind=shipments.DELIVERY_CARRIER,
        expected_items=list(reversed(expected)),
        rtus=list(reversed(rtus)),
        shipments=physical,
        source_revisions={"onec": "revision-2", "bitrix_sale": "revision-2"},
    )
    corrected = shipments.build_snapshot_id(
        site_order_number="242700",
        delivery_kind=shipments.DELIVERY_CARRIER,
        expected_items=expected,
        rtus=rtus,
        shipments=[{**physical[0], "status": shipments.STATUS_DISPATCHED}],
        source_revisions={"onec": "revision-3", "bitrix_sale": "revision-3"},
    )

    assert same_content == first
    assert corrected != first


def test_disappeared_rtu_and_shipment_are_retired_and_correction_clears_assembly(
    db_session,
) -> None:
    expected = [_line("phone", "1"), _line("case", "1")]
    rtus = [
        _rtu("rtu-1", [_line("phone", "1")]),
        _rtu("rtu-2", [_line("case", "1")]),
    ]
    physical = [
        _shipment(
            "shipment-1",
            [_line("phone", "1", rtu_external_id="rtu-1")],
            status=shipments.STATUS_READY,
        ),
        _shipment(
            "shipment-2",
            [_line("case", "1", rtu_external_id="rtu-2")],
            status=shipments.STATUS_READY,
        ),
    ]
    base = {
        "site_order_number": "242701",
        "bitrix_deal_id": 39701,
        "current_stage": "FINAL_INVOICE",
        "delivery_kind": shipments.DELIVERY_CARRIER,
        "expected_items": expected,
        "event_at": datetime(2026, 8, 29, 12, 0),
        "persist": True,
        "enqueue_crm_fields": True,
        "enqueue_notifications": False,
        "email_enabled": False,
        "sms_enabled": False,
    }
    shipments.sync_order_shipments(db_session, **base, rtus=rtus, shipments=physical)
    db_session.commit()

    corrected = shipments.sync_order_shipments(
        db_session,
        **{
            **base,
            "event_at": datetime(2026, 8, 29, 13, 0),
            "current_stage": "FINAL_INVOICE",
        },
        rtus=rtus[:1],
        shipments=physical[:1],
    )
    db_session.commit()

    retired_rtu = db_session.scalar(select(SiteOrderRtu).where(SiteOrderRtu.external_id == "rtu-2"))
    retired_shipment = db_session.scalar(
        select(SiteOrderShipment).where(SiteOrderShipment.shipment_key == "shipment-2")
    )
    crm_rows = db_session.scalars(
        select(SiteOrderFulfillmentOutbox)
        .where(SiteOrderFulfillmentOutbox.operation == shipments.OP_UPDATE_SHIPMENT_CRM_FIELDS)
        .order_by(SiteOrderFulfillmentOutbox.id)
    ).all()

    assert corrected.coverage_status == "partial"
    assert retired_rtu is not None and retired_rtu.active is False
    assert retired_rtu.retired_at == datetime(2026, 8, 29, 13, 0)
    assert retired_shipment is not None and retired_shipment.active is False
    assert retired_shipment.retired_at == datetime(2026, 8, 29, 13, 0)
    assert crm_rows[-1].payload["fields"][shipments.FULL_ASSEMBLY_FIELD] == ""
    assert crm_rows[-1].payload["fields"][shipments.FULL_ASSEMBLY_STATUS_FIELD] == "partial"

    client = _BitrixClient("242701", 39701)
    bot._dispatch_outbox(  # noqa: SLF001
        db_session,
        row=crm_rows[-1],
        client=client,
        settings=Settings(
            _env_file=None,
            order_fulfillment_bot_apply_enabled=True,
            order_fulfillment_shipments_master_enabled=True,
            order_fulfillment_shipments_crm_fields_enabled=True,
        ),
        onec_validator=lambda _: None,
        now=datetime(2026, 8, 29, 13, 5),
    )
    assert any(
        "Подтверждение полной сборки снято" in comment and "Статус: partial" in comment
        for comment in client.timeline_comments
    )


@pytest.mark.parametrize(
    ("line", "expected_reason"),
    [
        (_line("case", "1", rtu_external_id="missing"), "shipment_rtu_not_found_or_inactive"),
        (_line("case", "1", rtu_external_id="rtu-1"), "shipment_rtu_product_mismatch"),
        (_line("phone", "2", rtu_external_id="rtu-1"), "shipment_rtu_quantity_excess"),
    ],
)
def test_rtu_allocation_is_strict(line: dict, expected_reason: str) -> None:
    reason = shipments.validate_rtu_allocations(
        rtus=[_rtu("rtu-1", [_line("phone", "1")])],
        shipments=[_shipment("shipment-1", [line], status=shipments.STATUS_READY)],
    )

    assert reason == expected_reason


def test_internal_pickup_routes_to_pickup_transit() -> None:
    expected = [_line("phone", "1")]
    decision = shipments.derive_shipment_stage(
        current_stage="EXECUTING",
        coverage=shipments.evaluate_assembly_coverage(expected, expected),
        expected_lines=expected,
        shipments=[
            _shipment(
                "shipment-1",
                expected,
                status=shipments.STATUS_DISPATCHED,
                tracking="internal-trip",
            )
        ],
        delivery_kind=shipments.DELIVERY_INTERNAL_PICKUP,
    )

    assert decision.target_stage == "PICKUP_TRANSIT"


def test_gateway_outbox_requires_explicit_mutation(db_session) -> None:
    expected = [_line("phone", "1"), _line("case", "1")]
    physical = [
        {
            **_shipment(
                "shipment-1",
                [_line("phone", "1", rtu_external_id="rtu-1", basket_item_id=701)],
                status=shipments.STATUS_READY,
                tracking="existing-track",
            ),
            "delivery_service_id": 11,
        },
        {
            "shipment_key": "shipment-2",
            "delivery_service_id": 11,
            "explicit_split_confirmed": True,
            "status": shipments.STATUS_READY,
            "items": [_line("case", "1", rtu_external_id="rtu-1", basket_item_id=702)],
        },
    ]
    result = shipments.sync_order_shipments(
        db_session,
        site_order_number="242702",
        bitrix_deal_id=39702,
        bitrix_order_id=7702,
        current_stage="FINAL_INVOICE",
        delivery_kind=shipments.DELIVERY_CARRIER,
        source_revisions={"bitrix_sale": "bitrix-revision-1"},
        expected_items=expected,
        rtus=[_rtu("rtu-1", expected)],
        shipments=physical,
        event_at=datetime(2026, 8, 29, 12, 0),
        persist=True,
        enqueue_crm_fields=False,
        enqueue_notifications=False,
        enqueue_gateway=True,
        email_enabled=False,
        sms_enabled=False,
    )
    db_session.commit()
    rows = db_session.scalars(
        select(SiteOrderFulfillmentOutbox).where(
            SiteOrderFulfillmentOutbox.operation == shipments.OP_APPLY_SHIPMENT_GATEWAY
        )
    ).all()

    assert result.gateway_operation_count == 1
    assert len(rows) == 1
    assert rows[0].payload["action"] == "ensure"
    assert rows[0].payload["shipment_key"] == "shipment-2"
    assert rows[0].payload["source_order_revision"] == "bitrix-revision-1"

    tracking_update = shipments.sync_order_shipments(
        db_session,
        site_order_number="242702",
        bitrix_deal_id=39702,
        bitrix_order_id=7702,
        current_stage="FINAL_INVOICE",
        delivery_kind=shipments.DELIVERY_CARRIER,
        source_revisions={"bitrix_sale": "bitrix-revision-1"},
        expected_items=expected,
        rtus=[_rtu("rtu-1", expected)],
        shipments=[{**physical[0], "tracking_update_confirmed": True}, physical[1]],
        event_at=datetime(2026, 8, 29, 12, 5),
        persist=True,
        enqueue_crm_fields=False,
        enqueue_notifications=False,
        enqueue_gateway=True,
        email_enabled=False,
        sms_enabled=False,
    )
    db_session.commit()
    actions = db_session.scalars(
        select(SiteOrderFulfillmentOutbox)
        .where(SiteOrderFulfillmentOutbox.operation == shipments.OP_APPLY_SHIPMENT_GATEWAY)
        .order_by(SiteOrderFulfillmentOutbox.id)
    ).all()

    assert tracking_update.event_id is None
    assert tracking_update.gateway_operation_count == 1
    assert [item.payload["action"] for item in actions] == ["ensure", "update_tracking"]


def test_master_switch_blocks_all_shipment_outbox_effects(db_session) -> None:
    expected = [_line("phone", "1"), _line("case", "1")]
    shipments.sync_order_shipments(
        db_session,
        site_order_number="242703",
        bitrix_deal_id=39703,
        bitrix_order_id=7703,
        current_stage="FINAL_INVOICE",
        delivery_kind=shipments.DELIVERY_CARRIER,
        source_revisions={"bitrix_sale": "bitrix-revision-1"},
        expected_items=expected,
        rtus=[_rtu("rtu-1", expected)],
        shipments=[
            {
                **_shipment(
                    "shipment-1",
                    [_line("phone", "1", rtu_external_id="rtu-1", basket_item_id=701)],
                    status=shipments.STATUS_DISPATCHED,
                    tracking="track-1",
                ),
                "delivery_service_id": 11,
            },
            {
                "shipment_key": "shipment-2",
                "delivery_service_id": 11,
                "explicit_split_confirmed": True,
                "status": shipments.STATUS_READY,
                "items": [_line("case", "1", rtu_external_id="rtu-1", basket_item_id=702)],
            },
        ],
        event_at=datetime(2026, 8, 29, 12, 0),
        persist=True,
        enqueue_crm_fields=True,
        enqueue_notifications=True,
        enqueue_gateway=True,
        email_enabled=True,
        sms_enabled=False,
    )
    db_session.commit()
    rows = db_session.scalars(select(SiteOrderFulfillmentOutbox)).all()
    client = _BitrixClient("242703", 39703)
    settings = Settings(
        _env_file=None,
        order_fulfillment_bot_apply_enabled=False,
        order_fulfillment_shipments_master_enabled=True,
        order_fulfillment_shipments_crm_fields_enabled=True,
        order_fulfillment_shipments_notifications_enabled=True,
        order_fulfillment_shipments_gateway_apply_enabled=True,
        order_fulfillment_shipments_email_enabled=True,
        order_fulfillment_shipments_email_workflow_template_id=77,
        order_fulfillment_shipments_gateway_url="https://example.invalid",
        order_fulfillment_shipments_gateway_token="secret",
    )

    assert {row.operation for row in rows} == {
        shipments.OP_UPDATE_SHIPMENT_CRM_FIELDS,
        shipments.OP_START_SHIPMENT_NOTIFICATION,
        shipments.OP_APPLY_SHIPMENT_GATEWAY,
    }
    for row in rows:
        with pytest.raises(bot.ApplyDisabledBeforeSideEffect):
            bot._dispatch_outbox(  # noqa: SLF001
                db_session,
                row=row,
                client=client,
                settings=settings,
                onec_validator=lambda _: None,
                now=datetime(2026, 8, 29, 12, 5),
            )
    assert client.raw == {fulfillment.CRM_ORDER_NUMBER_FIELD: "242703"}
    assert client.workflows == []


@pytest.mark.parametrize(
    ("client_order", "stage_id", "expected_error"),
    [
        ("another-order", "PARTIALLY_SHIPPED", "shipment_gateway_deal_order_mismatch"),
        ("242703", "WON", "shipment_gateway_terminal_deal"),
    ],
)
def test_gateway_never_mutates_foreign_or_terminal_deal(
    db_session,
    client_order: str,
    stage_id: str,
    expected_error: str,
) -> None:
    expected = [_line("phone", "1")]
    shipments.sync_order_shipments(
        db_session,
        site_order_number="242703",
        bitrix_deal_id=39703,
        bitrix_order_id=7703,
        current_stage="FINAL_INVOICE",
        delivery_kind=shipments.DELIVERY_CARRIER,
        source_revisions={"bitrix_sale": "bitrix-revision-1"},
        expected_items=expected,
        rtus=[_rtu("rtu-1", expected)],
        shipments=[
            {
                "shipment_key": "shipment-1",
                "delivery_service_id": 11,
                "explicit_split_confirmed": True,
                "status": shipments.STATUS_READY,
                "items": [_line("phone", "1", rtu_external_id="rtu-1", basket_item_id=701)],
            }
        ],
        event_at=datetime(2026, 8, 29, 12, 0),
        persist=True,
        enqueue_crm_fields=False,
        enqueue_notifications=False,
        enqueue_gateway=True,
        email_enabled=False,
        sms_enabled=False,
    )
    db_session.commit()
    row = db_session.scalar(
        select(SiteOrderFulfillmentOutbox).where(
            SiteOrderFulfillmentOutbox.operation == shipments.OP_APPLY_SHIPMENT_GATEWAY
        )
    )
    assert row is not None

    with pytest.raises(RuntimeError, match=expected_error):
        bot._dispatch_outbox(  # noqa: SLF001
            db_session,
            row=row,
            client=_BitrixClient(client_order, 39703, stage_id=stage_id),
            settings=Settings(
                _env_file=None,
                order_fulfillment_bot_apply_enabled=True,
                order_fulfillment_shipments_master_enabled=True,
                order_fulfillment_shipments_gateway_apply_enabled=True,
                order_fulfillment_shipments_gateway_url="https://example.invalid",
                order_fulfillment_shipments_gateway_token="secret",
            ),
            onec_validator=lambda _: None,
            now=datetime(2026, 8, 29, 12, 5),
        )


def test_gateway_outbox_validates_live_order_and_updates_local_readback(
    db_session,
    monkeypatch,
) -> None:
    expected = [_line("phone", "1")]
    shipments.sync_order_shipments(
        db_session,
        site_order_number="242708",
        bitrix_deal_id=39708,
        bitrix_order_id=7708,
        current_stage="FINAL_INVOICE",
        delivery_kind=shipments.DELIVERY_CARRIER,
        source_revisions={"bitrix_sale": "bitrix-revision-1"},
        expected_items=expected,
        rtus=[_rtu("rtu-1", expected)],
        shipments=[
            {
                "shipment_key": "shipment-1",
                "delivery_service_id": 11,
                "explicit_split_confirmed": True,
                "status": shipments.STATUS_READY,
                "items": [_line("phone", "1", rtu_external_id="rtu-1", basket_item_id=701)],
            }
        ],
        event_at=datetime(2026, 8, 29, 12, 0),
        persist=True,
        enqueue_crm_fields=False,
        enqueue_notifications=False,
        enqueue_gateway=True,
        email_enabled=False,
        sms_enabled=False,
    )
    db_session.commit()
    row = db_session.scalar(
        select(SiteOrderFulfillmentOutbox).where(
            SiteOrderFulfillmentOutbox.operation == shipments.OP_APPLY_SHIPMENT_GATEWAY
        )
    )
    assert row is not None
    captured: dict = {}

    class Gateway:
        def __init__(self, **kwargs):
            captured["config"] = kwargs

        def get_order_snapshot(self, *, site_order_number):
            assert site_order_number == "242708"
            return {
                "order_id": 7708,
                "mutable": True,
                "revision": "current-live-revision",
                "shipments": [],
            }

        def ensure_shipment(self, **kwargs):
            captured["ensure"] = kwargs
            return {
                "ok": True,
                "shipment": {
                    "shipment_id": 88,
                    "revision": "shipment-revision-2",
                    "tracking_number": "",
                    "items": [{"basket_item_id": 701, "shipment_item_id": 801}],
                },
            }

    monkeypatch.setattr(shipments, "BitrixSaleShipmentGatewayClient", Gateway)

    bot._dispatch_outbox(  # noqa: SLF001
        db_session,
        row=row,
        client=_BitrixClient("242708", 39708),
        settings=Settings(
            _env_file=None,
            order_fulfillment_bot_apply_enabled=True,
            order_fulfillment_shipments_master_enabled=True,
            order_fulfillment_shipments_gateway_apply_enabled=True,
            order_fulfillment_shipments_gateway_url="https://example.invalid",
            order_fulfillment_shipments_gateway_token="secret",
        ),
        onec_validator=lambda _: None,
        now=datetime(2026, 8, 29, 12, 5),
    )

    local = db_session.scalar(
        select(SiteOrderShipment).where(SiteOrderShipment.shipment_key == "shipment-1")
    )
    assert local is not None
    assert captured["ensure"]["expected_order_revision"] == "bitrix-revision-1"
    assert captured["ensure"]["idempotency_key"] == row.idempotency_key
    assert local.bitrix_shipment_id == 88
    assert local.source_revision == "shipment-revision-2"
    assert local.items[0].bitrix_shipment_item_id == 801


def test_single_to_multi_keeps_first_part_owned_by_legacy_robot(db_session) -> None:
    expected = [_line("phone", "1"), _line("case", "1")]
    base = {
        "site_order_number": "242704",
        "bitrix_deal_id": 39704,
        "delivery_kind": shipments.DELIVERY_CARRIER,
        "expected_items": expected,
        "rtus": [_rtu("rtu-1", expected)],
        "persist": True,
        "enqueue_crm_fields": False,
        "enqueue_notifications": True,
        "email_enabled": True,
        "sms_enabled": False,
    }
    first = _shipment(
        "shipment-1",
        [_line("phone", "1", rtu_external_id="rtu-1")],
        status=shipments.STATUS_DISPATCHED,
        tracking="track-1",
    )
    shipments.sync_order_shipments(
        db_session,
        **base,
        current_stage="FINAL_INVOICE",
        shipments=[first],
        event_at=datetime(2026, 8, 29, 12, 0),
    )
    db_session.commit()
    second = _shipment(
        "shipment-2",
        [_line("case", "1", rtu_external_id="rtu-1")],
        status=shipments.STATUS_DISPATCHED,
        tracking="track-2",
    )
    result = shipments.sync_order_shipments(
        db_session,
        **base,
        current_stage="PARTIALLY_SHIPPED",
        shipments=[first, second],
        event_at=datetime(2026, 8, 29, 13, 0),
    )
    db_session.commit()
    persisted = {
        row.shipment_key: row for row in db_session.scalars(select(SiteOrderShipment)).all()
    }
    notifications = db_session.scalars(select(SiteOrderShipmentNotification)).all()

    assert result.notification_count == 1
    assert persisted["shipment-1"].legacy_owned is True
    assert persisted["shipment-1"].part_number == 1
    assert persisted["shipment-2"].part_number == 2
    assert [item.shipment.shipment_key for item in notifications] == ["shipment-2"]


def test_lost_notification_commit_is_recovered_from_marker(db_session) -> None:
    expected = [_line("phone", "1"), _line("case", "1")]
    shipments.sync_order_shipments(
        db_session,
        site_order_number="242705",
        bitrix_deal_id=39705,
        current_stage="FINAL_INVOICE",
        delivery_kind=shipments.DELIVERY_CARRIER,
        expected_items=expected,
        rtus=[_rtu("rtu-1", expected)],
        shipments=[
            _shipment(
                "shipment-1",
                [_line("phone", "1", rtu_external_id="rtu-1")],
                status=shipments.STATUS_DISPATCHED,
                tracking="track-1",
            ),
            _shipment(
                "shipment-2",
                [_line("case", "1", rtu_external_id="rtu-1")],
                status=shipments.STATUS_READY,
                tracking="track-2",
            ),
        ],
        event_at=datetime(2026, 8, 29, 12, 0),
        persist=True,
        enqueue_crm_fields=False,
        enqueue_notifications=True,
        email_enabled=True,
        sms_enabled=False,
    )
    db_session.commit()
    notification = db_session.scalar(select(SiteOrderShipmentNotification))
    row = db_session.scalar(
        select(SiteOrderFulfillmentOutbox).where(
            SiteOrderFulfillmentOutbox.operation == shipments.OP_START_SHIPMENT_NOTIFICATION
        )
    )
    assert notification is not None and row is not None
    client = _BitrixClient("242705", 39705)
    client.timeline_comments.append(
        bot._shipment_notification_marker(notification.idempotency_key)  # noqa: SLF001
    )

    bot._dispatch_outbox(  # noqa: SLF001
        db_session,
        row=row,
        client=client,
        settings=Settings(
            _env_file=None,
            order_fulfillment_bot_apply_enabled=True,
            order_fulfillment_shipments_master_enabled=True,
            order_fulfillment_shipments_notifications_enabled=True,
            order_fulfillment_shipments_email_enabled=True,
            order_fulfillment_shipments_email_workflow_template_id=77,
        ),
        onec_validator=lambda _: None,
        now=datetime(2026, 8, 29, 12, 5),
    )

    assert notification.status == "sent"
    assert notification.sent_at == datetime(2026, 8, 29, 12, 5)
    assert client.workflows == []


def test_stale_submitted_notification_is_rechecked_before_workflow_retry(db_session) -> None:
    expected = [_line("phone", "1"), _line("case", "1")]
    shipments.sync_order_shipments(
        db_session,
        site_order_number="242707",
        bitrix_deal_id=39707,
        current_stage="FINAL_INVOICE",
        delivery_kind=shipments.DELIVERY_CARRIER,
        expected_items=expected,
        rtus=[_rtu("rtu-1", expected)],
        shipments=[
            _shipment(
                "shipment-1",
                [_line("phone", "1", rtu_external_id="rtu-1")],
                status=shipments.STATUS_DISPATCHED,
                tracking="track-1",
            ),
            _shipment(
                "shipment-2",
                [_line("case", "1", rtu_external_id="rtu-1")],
                status=shipments.STATUS_READY,
                tracking="track-2",
            ),
        ],
        event_at=datetime(2026, 8, 29, 12, 0),
        persist=True,
        enqueue_crm_fields=False,
        enqueue_notifications=True,
        email_enabled=True,
        sms_enabled=False,
    )
    db_session.commit()
    notification = db_session.scalar(select(SiteOrderShipmentNotification))
    row = db_session.scalar(
        select(SiteOrderFulfillmentOutbox).where(
            SiteOrderFulfillmentOutbox.operation == shipments.OP_START_SHIPMENT_NOTIFICATION
        )
    )
    assert notification is not None and row is not None
    notification.status = "submitted"
    notification.submitted_at = datetime(2026, 8, 29, 12, 0)
    row.status = "completed"
    row.updated_at = datetime(2026, 8, 29, 12, 0)
    db_session.commit()
    client = _BitrixClient("242707", 39707)
    client.timeline_comments.append(
        bot._shipment_notification_marker(notification.idempotency_key)  # noqa: SLF001
    )

    stats = bot.process_outbox(
        db_session,
        client=client,
        settings=Settings(
            _env_file=None,
            order_fulfillment_bot_apply_enabled=True,
            order_fulfillment_shipments_master_enabled=True,
            order_fulfillment_shipments_notifications_enabled=True,
            order_fulfillment_shipments_email_enabled=True,
            order_fulfillment_shipments_email_workflow_template_id=77,
            order_fulfillment_shipments_notification_recovery_minutes=30,
        ),
        onec_validator=lambda _: None,
        now=datetime(2026, 8, 29, 12, 31),
    )

    db_session.refresh(notification)
    db_session.refresh(row)
    assert stats["recovered"] == 1
    assert notification.status == "sent"
    assert row.status == "completed"
    assert client.workflows == []


def test_notification_status_allows_failed_recovery_without_success_regression(
    db_session,
) -> None:
    expected = [_line("phone", "1"), _line("case", "1")]
    shipments.sync_order_shipments(
        db_session,
        site_order_number="242706",
        bitrix_deal_id=39706,
        current_stage="FINAL_INVOICE",
        delivery_kind=shipments.DELIVERY_CARRIER,
        expected_items=expected,
        rtus=[_rtu("rtu-1", expected)],
        shipments=[
            _shipment(
                "shipment-1",
                [_line("phone", "1", rtu_external_id="rtu-1")],
                status=shipments.STATUS_DISPATCHED,
                tracking="track-1",
            ),
            _shipment(
                "shipment-2",
                [_line("case", "1", rtu_external_id="rtu-1")],
                status=shipments.STATUS_READY,
                tracking="track-2",
            ),
        ],
        event_at=datetime(2026, 8, 29, 12, 0),
        persist=True,
        enqueue_crm_fields=False,
        enqueue_notifications=True,
        email_enabled=True,
        sms_enabled=False,
    )
    notification = db_session.scalar(select(SiteOrderShipmentNotification))
    assert notification is not None
    notification.status = "failed"
    db_session.flush()

    recovered = shipments.update_notification_status(
        db_session,
        idempotency_key=notification.idempotency_key,
        status="sent",
        occurred_at=datetime(2026, 8, 29, 12, 10),
    )
    late_failure = shipments.update_notification_status(
        db_session,
        idempotency_key=notification.idempotency_key,
        status="failed",
        occurred_at=datetime(2026, 8, 29, 12, 11),
        error="late callback",
    )

    assert recovered.changed is True
    assert late_failure.changed is False
    assert notification.status == "sent"
