from __future__ import annotations

from datetime import datetime

from app.core.config import Settings
from app.services import site_order_fulfillment as fulfillment
from app.services import site_order_shipment_poller as poller
from app.services import site_order_shipments as shipments


def _deal(delivery: str) -> fulfillment.BitrixDealSnapshot:
    return fulfillment.BitrixDealSnapshot(
        deal_id=41001,
        stage_id="FINAL_INVOICE",
        delivery=delivery,
        raw={fulfillment.CRM_ORDER_NUMBER_FIELD: "242800"},
    )


def _onec_snapshot() -> dict:
    return {
        "source_revision": "onec-revision-1",
        "expected_items": [{"product_ref": "product-a", "quantity": "2"}],
        "rtus": [
            {
                "external_id": "rtu-1",
                "posted": True,
                "assembled_at": datetime(2026, 8, 29, 10, 0),
                "source_revision": "rtu-revision-1",
                "items": [{"product_ref": "product-a", "quantity": "1"}],
            },
            {
                "external_id": "rtu-2",
                "posted": True,
                "assembled_at": datetime(2026, 8, 29, 11, 0),
                "source_revision": "rtu-revision-2",
                "items": [{"product_ref": "product-a", "quantity": "1"}],
            },
        ],
    }


def test_compose_snapshot_allocates_bitrix_items_across_rtus_fifo() -> None:
    bitrix_order = {
        "order_id": 8101,
        "revision": "bitrix-revision-1",
        "shipments": [
            {
                "shipment_id": 51,
                "shipment_key": "part-1",
                "delivery_service_id": 11,
                "deducted": True,
                "date_deducted": datetime(2026, 8, 29, 12, 0),
                "tracking_number": "track-1",
                "revision": "shipment-revision-1",
                "items": [
                    {
                        "shipment_item_id": 501,
                        "basket_item_id": 701,
                        "product_xml_id": "product-a",
                        "product_id": 901,
                        "quantity": "1.5",
                    }
                ],
            },
            {
                "shipment_id": 52,
                "shipment_key": "part-2",
                "delivery_service_id": 11,
                "deducted": False,
                "tracking_number": "track-2",
                "revision": "shipment-revision-2",
                "items": [
                    {
                        "shipment_item_id": 502,
                        "basket_item_id": 702,
                        "product_xml_id": "product-a",
                        "product_id": 901,
                        "quantity": "0.5",
                    }
                ],
            },
        ],
    }

    snapshot = poller.compose_snapshot(
        deal=_deal("СДЭК"),
        site_order_number="242800",
        onec_snapshot=_onec_snapshot(),
        bitrix_order=bitrix_order,
        observed_at=datetime(2026, 8, 29, 12, 5),
    )

    assert snapshot["delivery_kind"] == shipments.DELIVERY_CARRIER
    assert snapshot["source_revisions"] == {
        "onec": "onec-revision-1",
        "bitrix_sale": "bitrix-revision-1",
    }
    assert [item["rtu_external_id"] for item in snapshot["shipments"][0]["items"]] == [
        "rtu-1",
        "rtu-2",
    ]
    assert [item["quantity"] for item in snapshot["shipments"][0]["items"]] == [
        "1",
        "0.5",
    ]
    assert snapshot["shipments"][1]["items"][0]["rtu_external_id"] == "rtu-2"
    assert (
        shipments.validate_rtu_allocations(rtus=snapshot["rtus"], shipments=snapshot["shipments"])
        is None
    )


def test_compose_snapshot_routes_pickup_and_fails_closed_on_product_mismatch() -> None:
    snapshot = poller.compose_snapshot(
        deal=_deal("Самовывоз Митино"),
        site_order_number="242800",
        onec_snapshot=_onec_snapshot(),
        bitrix_order={
            "order_id": 8101,
            "revision": "bitrix-revision-1",
            "shipments": [
                {
                    "shipment_id": 51,
                    "shipment_key": "part-1",
                    "delivery_service_id": 11,
                    "deducted": True,
                    "tracking_number": "internal-trip",
                    "revision": "shipment-revision-1",
                    "items": [
                        {
                            "shipment_item_id": 501,
                            "basket_item_id": 701,
                            "product_xml_id": "another-product",
                            "product_id": 999,
                            "quantity": "1",
                        }
                    ],
                }
            ],
        },
        observed_at=datetime(2026, 8, 29, 12, 5),
    )

    assert snapshot["delivery_kind"] == shipments.DELIVERY_INTERNAL_PICKUP
    assert snapshot["shipments"][0]["status"] == shipments.STATUS_CONFLICT
    assert snapshot["shipments"][0]["items"][0]["rtu_external_id"] is None


def test_operational_metrics_expose_source_freshness_and_conflict_queues(db_session) -> None:
    metrics = poller.shipment_operational_metrics(db_session)

    assert metrics["shipment_onec_snapshot_latest_at"] is None
    assert metrics["shipment_bitrix_snapshot_latest_at"] is None
    assert metrics["shipment_onec_snapshot_age_seconds"] is None
    assert metrics["shipment_bitrix_snapshot_age_seconds"] is None
    assert metrics["shipment_rtu_allocation_conflicts"] == 0
    assert metrics["shipment_gateway_conflicts"] == 0
    assert metrics["shipment_outbox_stuck"] == 0


def test_configuration_readiness_fails_closed_for_missing_workflow_contract() -> None:
    issues = poller.shipment_configuration_issues(
        Settings(
            _env_file=None,
            order_fulfillment_shipments_master_enabled=True,
            order_fulfillment_shipments_notifications_enabled=True,
            order_fulfillment_shipments_email_enabled=True,
            order_fulfillment_shipments_email_workflow_template_id=None,
        )
    )

    assert issues == ["shipment_email_workflow_template_missing"]
