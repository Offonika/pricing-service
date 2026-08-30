from __future__ import annotations

from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Mapping

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    LogisticsManualReview,
    SiteOrderExecutionCase,
    SiteOrderFulfillmentOutbox,
    SiteOrderRtu,
    SiteOrderShipment,
    SiteOrderShipmentNotification,
    SiteOrderStageOutbox,
)
from app.services import site_order_fulfillment as fulfillment
from app.services import site_order_shipments as shipment_service


def shipment_configuration_issues(settings: Any) -> list[str]:
    issues: list[str] = []
    if settings.order_fulfillment_shipments_poller_enabled:
        if not settings.onec_database_url:
            issues.append("shipment_poller_onec_database_missing")
        if not settings.order_fulfillment_shipments_gateway_url:
            issues.append("shipment_poller_gateway_url_missing")
        if not settings.order_fulfillment_shipments_gateway_token:
            issues.append("shipment_poller_gateway_token_missing")
    if settings.order_fulfillment_shipments_gateway_apply_enabled:
        if not settings.order_fulfillment_shipments_gateway_url:
            issues.append("shipment_gateway_url_missing")
        if not settings.order_fulfillment_shipments_gateway_token:
            issues.append("shipment_gateway_token_missing")
    if settings.order_fulfillment_shipments_notifications_enabled:
        if not (
            settings.order_fulfillment_shipments_email_enabled
            or settings.order_fulfillment_shipments_sms_enabled
        ):
            issues.append("shipment_notification_channel_missing")
        if (
            settings.order_fulfillment_shipments_email_enabled
            and settings.order_fulfillment_shipments_email_workflow_template_id is None
        ):
            issues.append("shipment_email_workflow_template_missing")
        if (
            settings.order_fulfillment_shipments_sms_enabled
            and settings.order_fulfillment_shipments_sms_workflow_template_id is None
        ):
            issues.append("shipment_sms_workflow_template_missing")
    if (
        any(
            (
                settings.order_fulfillment_shipments_ingest_enabled,
                settings.order_fulfillment_shipments_crm_fields_enabled,
                settings.order_fulfillment_shipments_stage_apply_enabled,
                settings.order_fulfillment_shipments_gateway_apply_enabled,
                settings.order_fulfillment_shipments_notifications_enabled,
            )
        )
        and not settings.order_fulfillment_shipments_master_enabled
    ):
        issues.append("shipment_master_disabled_for_enabled_subfeature")
    return issues


def delivery_kind_from_deal(deal: fulfillment.BitrixDealSnapshot) -> str:
    raw = " ".join(value for value in (deal.delivery, deal.post_delivery_type) if value)
    delivery_class = fulfillment.classify_delivery_method(raw)
    if delivery_class == fulfillment.DELIVERY_CLASS_PICKUP:
        return shipment_service.DELIVERY_INTERNAL_PICKUP
    if delivery_class in {
        fulfillment.DELIVERY_CLASS_CARRIER,
        fulfillment.DELIVERY_CLASS_COURIER,
    }:
        return shipment_service.DELIVERY_CARRIER
    return shipment_service.DELIVERY_UNKNOWN


def compose_snapshot(
    *,
    deal: fulfillment.BitrixDealSnapshot,
    site_order_number: str,
    onec_snapshot: Mapping[str, Any],
    bitrix_order: Mapping[str, Any],
    observed_at: datetime,
) -> dict[str, Any]:
    expected_items = [dict(item) for item in onec_snapshot.get("expected_items") or []]
    rtus = [dict(item) for item in onec_snapshot.get("rtus") or []]
    delivery_kind = delivery_kind_from_deal(deal)
    shipments = _compose_shipments(bitrix_order=bitrix_order, rtus=rtus)
    source_revisions = {
        "onec": str(onec_snapshot.get("source_revision") or ""),
        "bitrix_sale": str(bitrix_order.get("revision") or ""),
    }
    snapshot_id = shipment_service.build_snapshot_id(
        site_order_number=site_order_number,
        delivery_kind=delivery_kind,
        expected_items=expected_items,
        rtus=rtus,
        shipments=shipments,
        source_revisions=source_revisions,
    )
    return {
        "snapshot_id": snapshot_id,
        "site_order_number": site_order_number,
        "bitrix_deal_id": deal.deal_id,
        "bitrix_order_id": int(bitrix_order.get("order_id") or 0) or None,
        "current_stage": deal.stage_id,
        "delivery_kind": delivery_kind,
        "event_at": observed_at,
        "observed_at": observed_at,
        "source_revisions": source_revisions,
        "expected_items": expected_items,
        "rtus": rtus,
        "shipments": shipments,
    }


def _compose_shipments(
    *,
    bitrix_order: Mapping[str, Any],
    rtus: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    pools: dict[str, deque[list[Any]]] = defaultdict(deque)
    for rtu in sorted(rtus, key=lambda item: str(item.get("external_id") or "")):
        if not (rtu.get("posted") and rtu.get("assembled_at") and not rtu.get("cancelled_at")):
            continue
        for item in rtu.get("items") or []:
            product_ref = str(item.get("product_ref") or "").strip()
            if product_ref:
                pools[product_ref].append(
                    [str(rtu.get("external_id") or ""), Decimal(str(item.get("quantity") or 0))]
                )

    raw_shipments = [
        (index, raw)
        for index, raw in enumerate(bitrix_order.get("shipments") or [])
        if isinstance(raw, Mapping)
    ]

    def operational_priority(raw: Mapping[str, Any]) -> int:
        if bool(raw.get("canceled")):
            return 3
        if bool(raw.get("deducted")):
            return 0
        if str(raw.get("tracking_number") or "").strip():
            return 1
        return 2

    # A standard planned Bitrix shipment often contains the whole order. Allocate
    # actual/ready parts first so that the placeholder cannot consume assembled RTU
    # quantities which belong to a physical shipment.
    composed: dict[int, dict[str, Any]] = {}
    for index, raw in sorted(
        raw_shipments,
        key=lambda item: (operational_priority(item[1]), item[0]),
    ):
        items: list[dict[str, Any]] = []
        allocation_conflict = False
        canceled = bool(raw.get("canceled"))
        for item in raw.get("items") or []:
            if not isinstance(item, Mapping):
                continue
            product_ref = str(item.get("product_xml_id") or item.get("product_id") or "").strip()
            remaining = Decimal(str(item.get("quantity") or 0))
            while (
                not canceled
                and remaining > shipment_service.QUANTITY_TOLERANCE
                and pools[product_ref]
            ):
                rtu_external_id, available = pools[product_ref][0]
                take = min(remaining, available)
                items.append(
                    {
                        "product_ref": product_ref,
                        "product_code": str(item.get("product_id") or "") or None,
                        "quantity": str(take),
                        "rtu_external_id": rtu_external_id,
                        "basket_item_id": item.get("basket_item_id"),
                        "bitrix_shipment_item_id": item.get("shipment_item_id"),
                    }
                )
                remaining -= take
                available -= take
                if available <= shipment_service.QUANTITY_TOLERANCE:
                    pools[product_ref].popleft()
                else:
                    pools[product_ref][0][1] = available
            if remaining > shipment_service.QUANTITY_TOLERANCE:
                allocation_conflict = True
                items.append(
                    {
                        "product_ref": product_ref,
                        "product_code": str(item.get("product_id") or "") or None,
                        "quantity": str(remaining),
                        "rtu_external_id": None,
                        "basket_item_id": item.get("basket_item_id"),
                        "bitrix_shipment_item_id": item.get("shipment_item_id"),
                    }
                )
        deducted = bool(raw.get("deducted"))
        if canceled:
            status = shipment_service.STATUS_CONFLICT
        elif deducted:
            status = shipment_service.STATUS_DISPATCHED
        elif str(raw.get("tracking_number") or "").strip():
            status = shipment_service.STATUS_READY
        else:
            status = shipment_service.STATUS_PLANNED
        if allocation_conflict and status != shipment_service.STATUS_PLANNED:
            status = shipment_service.STATUS_CONFLICT
        composed[index] = {
            "shipment_key": str(raw.get("shipment_key") or "").strip()
            or f"bitrix:{raw.get('shipment_id')}",
            "bitrix_shipment_id": raw.get("shipment_id"),
            "delivery_service_id": raw.get("delivery_service_id"),
            "carrier": raw.get("delivery_service_id"),
            "tracking_number": raw.get("tracking_number"),
            "status": status,
            "dispatched_at": raw.get("date_deducted") if deducted else None,
            "source_revision": raw.get("revision"),
            "items": items,
        }
    return [composed[index] for index, _raw in raw_shipments]


def shipment_operational_metrics(session: Session) -> dict[str, Any]:
    now = datetime.now(UTC).replace(tzinfo=None)
    status_rows = session.execute(
        select(SiteOrderShipment.status, func.count(SiteOrderShipment.id))
        .where(SiteOrderShipment.active.is_(True))
        .group_by(SiteOrderShipment.status)
    ).all()
    notification_rows = session.execute(
        select(
            SiteOrderShipmentNotification.status,
            func.count(SiteOrderShipmentNotification.id),
        ).group_by(SiteOrderShipmentNotification.status)
    ).all()
    outbox_rows = session.execute(
        select(SiteOrderFulfillmentOutbox.status, func.count(SiteOrderFulfillmentOutbox.id))
        .where(
            SiteOrderFulfillmentOutbox.operation.in_(
                [
                    shipment_service.OP_UPDATE_SHIPMENT_CRM_FIELDS,
                    shipment_service.OP_START_SHIPMENT_NOTIFICATION,
                    shipment_service.OP_APPLY_SHIPMENT_GATEWAY,
                ]
            )
        )
        .group_by(SiteOrderFulfillmentOutbox.status)
    ).all()
    delivery_kinds: defaultdict[str, int] = defaultdict(int)
    latest_observed_at: datetime | None = None
    for payload in session.scalars(select(SiteOrderExecutionCase.payload)).all():
        control = payload.get("shipment_control") if isinstance(payload, dict) else None
        if isinstance(control, dict) and control.get("delivery_kind"):
            delivery_kinds[str(control["delivery_kind"])] += 1
        if isinstance(control, dict) and control.get("observed_at"):
            try:
                observed = datetime.fromisoformat(str(control["observed_at"]))
            except ValueError:
                continue
            observed = (
                observed.astimezone(UTC).replace(tzinfo=None)
                if observed.tzinfo is not None
                else observed
            )
            if latest_observed_at is None or observed > latest_observed_at:
                latest_observed_at = observed
    return {
        "shipments_active": sum(int(count) for _, count in status_rows),
        "shipments_retired": int(
            session.scalar(
                select(func.count(SiteOrderShipment.id)).where(SiteOrderShipment.active.is_(False))
            )
            or 0
        ),
        "rtus_retired": int(
            session.scalar(
                select(func.count(SiteOrderRtu.id)).where(SiteOrderRtu.active.is_(False))
            )
            or 0
        ),
        "shipments_without_bitrix_id": int(
            session.scalar(
                select(func.count(SiteOrderShipment.id)).where(
                    SiteOrderShipment.active.is_(True),
                    SiteOrderShipment.bitrix_shipment_id.is_(None),
                )
            )
            or 0
        ),
        "shipments_without_tracking": int(
            session.scalar(
                select(func.count(SiteOrderShipment.id)).where(
                    SiteOrderShipment.active.is_(True),
                    SiteOrderShipment.status.in_(["dispatched", "delivered"]),
                    SiteOrderShipment.tracking_number.is_(None),
                )
            )
            or 0
        ),
        "shipment_statuses": {str(status): int(count) for status, count in status_rows},
        "shipment_notifications": {str(status): int(count) for status, count in notification_rows},
        "shipment_outbox": {str(status): int(count) for status, count in outbox_rows},
        "shipment_conflicts": int(
            session.scalar(
                select(func.count(LogisticsManualReview.id)).where(
                    LogisticsManualReview.review_type.in_(
                        ["site_order_shipment_conflict", "site_order_shipment_gateway"]
                    )
                )
            )
            or 0
        ),
        "shipment_rtu_allocation_conflicts": int(
            session.scalar(
                select(func.count(LogisticsManualReview.id)).where(
                    LogisticsManualReview.review_type == "site_order_shipment_conflict",
                    LogisticsManualReview.reason.like("shipment_rtu_%"),
                )
            )
            or 0
        ),
        "shipment_gateway_conflicts": int(
            session.scalar(
                select(func.count(SiteOrderFulfillmentOutbox.id)).where(
                    SiteOrderFulfillmentOutbox.operation
                    == shipment_service.OP_APPLY_SHIPMENT_GATEWAY,
                    SiteOrderFulfillmentOutbox.status == "failed",
                )
            )
            or 0
        ),
        "shipment_outbox_stuck": int(
            session.scalar(
                select(func.count(SiteOrderFulfillmentOutbox.id)).where(
                    SiteOrderFulfillmentOutbox.operation.in_(
                        [
                            shipment_service.OP_UPDATE_SHIPMENT_CRM_FIELDS,
                            shipment_service.OP_START_SHIPMENT_NOTIFICATION,
                            shipment_service.OP_APPLY_SHIPMENT_GATEWAY,
                        ]
                    ),
                    SiteOrderFulfillmentOutbox.status.in_(["pending", "retry", "processing"]),
                    SiteOrderFulfillmentOutbox.updated_at <= now - timedelta(minutes=30),
                )
            )
            or 0
        ),
        "shipment_stage_pending": int(
            session.scalar(
                select(func.count(SiteOrderStageOutbox.id)).where(
                    SiteOrderStageOutbox.source_event_type == "shipment_snapshot_reconciled",
                    SiteOrderStageOutbox.status.in_(["pending", "retry", "manual_review"]),
                )
            )
            or 0
        ),
        "shipment_delivery_kinds": dict(sorted(delivery_kinds.items())),
        "shipment_snapshot_latest_at": (
            latest_observed_at.isoformat() if latest_observed_at is not None else None
        ),
        "shipment_onec_snapshot_latest_at": (
            latest_observed_at.isoformat() if latest_observed_at is not None else None
        ),
        "shipment_bitrix_snapshot_latest_at": (
            latest_observed_at.isoformat() if latest_observed_at is not None else None
        ),
        "shipment_snapshot_age_seconds": (
            max(
                0,
                int((now - latest_observed_at).total_seconds()),
            )
            if latest_observed_at is not None
            else None
        ),
        "shipment_onec_snapshot_age_seconds": (
            max(0, int((now - latest_observed_at).total_seconds()))
            if latest_observed_at is not None
            else None
        ),
        "shipment_bitrix_snapshot_age_seconds": (
            max(0, int((now - latest_observed_at).total_seconds()))
            if latest_observed_at is not None
            else None
        ),
    }
