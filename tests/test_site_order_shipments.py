from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select

from app.core.config import Settings
from app.models import (
    SiteOrderFulfillmentOutbox,
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
    def __init__(self, order_number: str, deal_id: int) -> None:
        self.order_number = order_number
        self.deal_id = deal_id
        self.raw = {fulfillment.CRM_ORDER_NUMBER_FIELD: order_number}
        self.workflows: list[dict] = []

    def get_deal_by_id(self, deal_id: int):
        if deal_id != self.deal_id:
            return None
        return fulfillment.BitrixDealSnapshot(
            deal_id=deal_id,
            stage_id="PARTIALLY_SHIPPED",
            raw=dict(self.raw),
        )

    def update_deal_fields(self, deal_id: int, fields: dict):
        assert deal_id == self.deal_id
        self.raw.update(fields)
        return True

    def start_business_process(self, **payload):
        self.workflows.append(payload)
        return "workflow-1"


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


def test_explicit_split_creates_only_missing_bitrix_shipment() -> None:
    class Gateway:
        calls: list[dict] = []

        def list_shipments(self, **kwargs):
            self.calls.append({"list": kwargs})
            return [{"shipment_id": 51, "tracking_number": ""}]

        def ensure_shipment(self, **kwargs):
            self.calls.append(kwargs)
            return {
                "ok": True,
                "created": True,
                "shipment": {
                    "shipment_id": 52,
                    "items": [
                        {
                            "basket_item_id": 702,
                            "shipment_item_id": 802,
                            "quantity": "1.0000",
                        }
                    ],
                },
            }

    gateway = Gateway()
    snapshots = shipments.ensure_missing_bitrix_shipments(
        gateway,
        order_id=7001,
        shipment_snapshots=[
            {
                "shipment_key": "part-1",
                "bitrix_shipment_id": 51,
                "delivery_service_id": 11,
                "items": [_line("phone", "1", basket_item_id=701)],
            },
            {
                "shipment_key": "part-2",
                "delivery_service_id": 11,
                "items": [_line("case", "1", basket_item_id=702)],
            },
        ],
    )

    assert len(gateway.calls) == 2
    assert gateway.calls[1]["shipment_key"] == "part-2"
    assert snapshots[0]["bitrix_shipment_id"] == 51
    assert snapshots[1]["bitrix_shipment_id"] == 52
    assert snapshots[1]["items"][0]["bitrix_shipment_item_id"] == 802


def test_split_tracking_is_updated_only_on_exact_shipment_id() -> None:
    class Gateway:
        updated: list[dict] = []

        def list_shipments(self, **kwargs):
            del kwargs
            return [
                {"shipment_id": 51, "tracking_number": "track-1", "revision": 10},
                {"shipment_id": 52, "tracking_number": "", "revision": 11},
            ]

        def update_tracking(self, **kwargs):
            self.updated.append(kwargs)
            return {
                "ok": True,
                "shipment": {
                    "shipment_id": kwargs["shipment_id"],
                    "tracking_number": kwargs["tracking_number"],
                },
            }

    gateway = Gateway()
    result = shipments.ensure_missing_bitrix_shipments(
        gateway,
        order_id=7001,
        shipment_snapshots=[
            {
                "shipment_key": "part-1",
                "bitrix_shipment_id": 51,
                "tracking_number": "track-1",
                "items": [_line("phone", "1", basket_item_id=701)],
            },
            {
                "shipment_key": "part-2",
                "bitrix_shipment_id": 52,
                "tracking_number": "track-2",
                "items": [_line("case", "1", basket_item_id=702)],
            },
        ],
    )

    assert len(result) == 2
    assert gateway.updated == [
        {
            "shipment_id": 52,
            "tracking_number": "track-2",
            "expected_revision": 11,
        }
    ]


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
    )
    complete = shipments.derive_shipment_stage(
        current_stage="PARTIALLY_SHIPPED",
        coverage=coverage,
        expected_lines=expected,
        shipments=[first, second_dispatched],
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
