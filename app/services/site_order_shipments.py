from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    LogisticsManualReview,
    SiteOrderExecutionCase,
    SiteOrderFulfillmentOutbox,
    SiteOrderRtu,
    SiteOrderRtuItem,
    SiteOrderShipment,
    SiteOrderShipmentItem,
    SiteOrderShipmentNotification,
    SiteOrderStageOutbox,
)
from app.services import site_order_fulfillment as fulfillment

FULL_ASSEMBLY_FIELD = "UF_CRM_MM_FULL_ASSEMBLY_CONFIRMED_AT"
FULL_ASSEMBLY_STATUS_FIELD = "UF_CRM_MM_FULL_ASSEMBLY_STATUS"
SHIPMENT_COUNT_FIELD = "UF_CRM_MM_SHIPMENT_COUNT"

DELIVERY_CARRIER = "carrier"
DELIVERY_INTERNAL_PICKUP = "internal_pickup"
DELIVERY_UNKNOWN = "unknown"

STATUS_PLANNED = "planned"
STATUS_READY = "ready"
STATUS_DISPATCHED = "dispatched"
STATUS_DELIVERED = "delivered"
STATUS_RETURNED = "returned"
STATUS_CONFLICT = "conflict"

CHANNEL_EMAIL = "email"
CHANNEL_SMS = "sms"
EVENT_DISPATCHED = "dispatched"

OP_UPDATE_SHIPMENT_CRM_FIELDS = "update_shipment_crm_fields"
OP_START_SHIPMENT_NOTIFICATION = "start_shipment_notification"
OP_APPLY_SHIPMENT_GATEWAY = "apply_shipment_gateway"

QUANTITY_TOLERANCE = Decimal("0.0001")


class ShipmentGatewayError(RuntimeError):
    pass


class BitrixSaleShipmentGatewayClient:
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        timeout: float = 30.0,
        session: requests.Session | None = None,
    ) -> None:
        self._base_url = base_url.strip()
        self._token = token.strip()
        self._timeout = timeout
        self._session = session or requests.Session()
        self._session.trust_env = False
        if not self._base_url or not self._token:
            raise ValueError("shipment_gateway_not_configured")

    def call(self, action: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        response = self._session.post(
            self._base_url,
            json={"action": action, **dict(payload)},
            headers={
                "Authorization": f"Bearer {self._token}",
                "X-MM-Shipment-Token": self._token,
                "Content-Type": "application/json",
            },
            timeout=self._timeout,
        )
        try:
            data = response.json()
        except ValueError as exc:
            raise ShipmentGatewayError(
                f"shipment_gateway_invalid_json:http_{response.status_code}"
            ) from exc
        if response.status_code >= 400 or not data.get("ok"):
            error = _clean(data.get("error")) or f"http_{response.status_code}"
            raise ShipmentGatewayError(f"shipment_gateway_error:{error}")
        return data

    def list_shipments(self, *, order_id: int) -> list[dict[str, Any]]:
        result = self.call("list", {"order_id": order_id})
        shipments = result.get("shipments")
        if not isinstance(shipments, list):
            raise ShipmentGatewayError("shipment_gateway_list_invalid")
        return [item for item in shipments if isinstance(item, dict)]

    def get_order_snapshot(self, *, site_order_number: str) -> dict[str, Any]:
        result = self.call("snapshot", {"site_order_number": site_order_number})
        order = result.get("order")
        if not isinstance(order, dict):
            raise ShipmentGatewayError("shipment_gateway_snapshot_invalid")
        return order

    def ensure_shipment(
        self,
        *,
        order_id: int,
        shipment_key: str,
        delivery_service_id: int,
        items: list[dict[str, Any]],
        site_order_number: str | None = None,
        expected_order_revision: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "order_id": order_id,
            "shipment_key": shipment_key,
            "delivery_service_id": delivery_service_id,
            "items": items,
        }
        if site_order_number:
            payload["site_order_number"] = site_order_number
        if expected_order_revision:
            payload["expected_order_revision"] = expected_order_revision
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key
        return self.call(
            "ensure",
            payload,
        )

    def update_tracking(
        self,
        *,
        shipment_id: int,
        tracking_number: str,
        expected_revision: int | str | None = None,
        site_order_number: str | None = None,
        expected_order_revision: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "shipment_id": shipment_id,
            "tracking_number": tracking_number,
            "expected_revision": expected_revision,
        }
        if site_order_number:
            payload["site_order_number"] = site_order_number
        if expected_order_revision:
            payload["expected_order_revision"] = expected_order_revision
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key
        return self.call(
            "update_tracking",
            payload,
        )


@dataclass(frozen=True, slots=True)
class AssemblyCoverage:
    status: str
    complete: bool
    expected_quantity: Decimal
    assembled_quantity: Decimal
    expected_by_product: dict[str, Decimal]
    assembled_by_product: dict[str, Decimal]
    missing_by_product: dict[str, Decimal]
    excess_by_product: dict[str, Decimal]


@dataclass(frozen=True, slots=True)
class ShipmentStageDecision:
    action: str
    target_stage: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class ShipmentSyncResult:
    snapshot_id: str
    site_order_number: str
    coverage_status: str
    full_assembly: bool
    shipment_count: int
    target_stage: str | None
    action: str
    reason: str
    event_id: int | None = None
    stage_outbox_id: int | None = None
    notification_count: int = 0
    gateway_operation_count: int = 0
    conflict: bool = False


@dataclass(frozen=True, slots=True)
class ShipmentNotificationStatusResult:
    idempotency_key: str
    status: str
    changed: bool


def _quantity(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"invalid_quantity:{value}") from exc
    if result < 0:
        raise ValueError(f"negative_quantity:{value}")
    return result.quantize(Decimal("0.0001"))


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _aggregate_lines(lines: Iterable[Mapping[str, Any]]) -> dict[str, Decimal]:
    totals: defaultdict[str, Decimal] = defaultdict(Decimal)
    for line in lines:
        product_ref = _clean(line.get("product_ref"))
        if not product_ref:
            raise ValueError("missing_product_ref")
        totals[product_ref] += _quantity(line.get("quantity"))
    return dict(sorted(totals.items()))


def evaluate_assembly_coverage(
    expected_lines: Iterable[Mapping[str, Any]],
    assembled_lines: Iterable[Mapping[str, Any]],
) -> AssemblyCoverage:
    expected = _aggregate_lines(expected_lines)
    assembled = _aggregate_lines(assembled_lines)
    if not expected:
        return AssemblyCoverage(
            status="unavailable",
            complete=False,
            expected_quantity=Decimal("0"),
            assembled_quantity=sum(assembled.values(), Decimal("0")),
            expected_by_product=expected,
            assembled_by_product=assembled,
            missing_by_product={},
            excess_by_product={},
        )

    missing: dict[str, Decimal] = {}
    excess: dict[str, Decimal] = {}
    for product_ref in sorted(set(expected) | set(assembled)):
        expected_qty = expected.get(product_ref, Decimal("0"))
        assembled_qty = assembled.get(product_ref, Decimal("0"))
        difference = expected_qty - assembled_qty
        if difference > QUANTITY_TOLERANCE:
            missing[product_ref] = difference
        elif difference < -QUANTITY_TOLERANCE:
            excess[product_ref] = -difference
    status = "conflict" if excess else "partial" if missing else "complete"
    return AssemblyCoverage(
        status=status,
        complete=status == "complete",
        expected_quantity=sum(expected.values(), Decimal("0")),
        assembled_quantity=sum(assembled.values(), Decimal("0")),
        expected_by_product=expected,
        assembled_by_product=assembled,
        missing_by_product=missing,
        excess_by_product=excess,
    )


def derive_shipment_stage(
    *,
    current_stage: str | None,
    coverage: AssemblyCoverage,
    expected_lines: Iterable[Mapping[str, Any]],
    shipments: Iterable[Mapping[str, Any]],
    delivery_kind: str = DELIVERY_UNKNOWN,
) -> ShipmentStageDecision:
    current = _clean(current_stage).upper()
    if (
        current in fulfillment.TERMINAL_CRM_STAGES
        or current.endswith(":WON")
        or current.endswith(":LOSE")
    ):
        return ShipmentStageDecision("noop", None, "terminal_crm_stage")
    if coverage.status in {"unavailable", "conflict"}:
        return ShipmentStageDecision("manual_review", None, f"assembly_{coverage.status}")

    expected = _aggregate_lines(expected_lines)
    shipment_list = list(shipments)
    identity_keys = [
        _clean(shipment.get("shipment_key"))
        or (
            f"bitrix:{shipment.get('bitrix_shipment_id')}"
            if shipment.get("bitrix_shipment_id") not in (None, "")
            else ""
        )
        for shipment in shipment_list
    ]
    if any(not key for key in identity_keys):
        return ShipmentStageDecision("manual_review", None, "shipment_identity_missing")
    if len(identity_keys) != len(set(identity_keys)):
        return ShipmentStageDecision("manual_review", None, "shipment_identity_duplicate")
    tracking_numbers = [_clean(shipment.get("tracking_number")) for shipment in shipment_list]
    nonempty_tracking = [value for value in tracking_numbers if value]
    if len(nonempty_tracking) != len(set(nonempty_tracking)):
        return ShipmentStageDecision("manual_review", None, "shipment_tracking_duplicate")

    dispatched_lines: list[Mapping[str, Any]] = []
    allocated_lines: list[Mapping[str, Any]] = []
    returned = False
    for shipment in shipment_list:
        status = _clean(shipment.get("status")).lower() or STATUS_PLANNED
        if status not in {
            STATUS_PLANNED,
            STATUS_READY,
            STATUS_DISPATCHED,
            STATUS_DELIVERED,
            STATUS_RETURNED,
            STATUS_CONFLICT,
        }:
            return ShipmentStageDecision("manual_review", None, "shipment_status_unknown")
        if status == STATUS_CONFLICT:
            return ShipmentStageDecision("manual_review", None, "shipment_marked_conflict")
        if status == STATUS_RETURNED:
            returned = True
        if status in {STATUS_READY, STATUS_DISPATCHED, STATUS_DELIVERED}:
            allocated_lines.extend(list(shipment.get("items") or []))
        if status in {STATUS_DISPATCHED, STATUS_DELIVERED, STATUS_RETURNED}:
            dispatched_lines.extend(list(shipment.get("items") or []))
    if returned:
        return ShipmentStageDecision("manual_review", None, "shipment_return_requires_review")

    allocated = _aggregate_lines(allocated_lines)
    allocation_excess = any(
        allocated.get(product_ref, Decimal("0")) - expected.get(product_ref, Decimal("0"))
        > QUANTITY_TOLERANCE
        for product_ref in set(expected) | set(allocated)
    )
    if allocation_excess:
        return ShipmentStageDecision("manual_review", None, "shipment_allocation_excess")

    dispatched = _aggregate_lines(dispatched_lines)
    exceeds_assembled = any(
        dispatched.get(product_ref, Decimal("0"))
        - coverage.assembled_by_product.get(product_ref, Decimal("0"))
        > QUANTITY_TOLERANCE
        for product_ref in set(coverage.assembled_by_product) | set(dispatched)
    )
    if exceeds_assembled:
        return ShipmentStageDecision("manual_review", None, "shipment_quantity_exceeds_assembled")
    excess = any(
        dispatched.get(product_ref, Decimal("0")) - expected.get(product_ref, Decimal("0"))
        > QUANTITY_TOLERANCE
        for product_ref in set(expected) | set(dispatched)
    )
    if excess:
        return ShipmentStageDecision("manual_review", None, "shipment_quantity_excess")
    dispatched_total = sum(dispatched.values(), Decimal("0"))
    expected_total = sum(expected.values(), Decimal("0"))
    all_dispatched = expected_total > 0 and all(
        abs(dispatched.get(product_ref, Decimal("0")) - quantity) <= QUANTITY_TOLERANCE
        for product_ref, quantity in expected.items()
    )
    if all_dispatched:
        if delivery_kind == DELIVERY_CARRIER:
            target = "IN_DELIVERY"
            reason = "all_order_quantities_dispatched"
        elif delivery_kind == DELIVERY_INTERNAL_PICKUP:
            target = "PICKUP_TRANSIT"
            reason = "all_order_quantities_sent_to_pickup_point"
        else:
            return ShipmentStageDecision("manual_review", None, "delivery_kind_unknown")
    elif dispatched_total > QUANTITY_TOLERANCE:
        target = "PARTIALLY_SHIPPED"
        reason = "some_order_quantities_dispatched"
    elif coverage.complete:
        target = "FINAL_INVOICE"
        reason = "all_order_quantities_assembled"
    else:
        target = "EXECUTING"
        reason = "waiting_for_full_assembly"
    if current == target:
        return ShipmentStageDecision("noop", None, f"already_{target.lower()}")
    if target == "EXECUTING" and current not in {"", "EXECUTING"}:
        return ShipmentStageDecision("manual_review", None, "later_stage_without_full_assembly")
    return ShipmentStageDecision("update_stage", target, reason)


def _fingerprint(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_ready(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    return value


def build_snapshot_id(
    *,
    site_order_number: str,
    delivery_kind: str,
    expected_items: list[Mapping[str, Any]],
    rtus: list[Mapping[str, Any]],
    shipments: list[Mapping[str, Any]],
    source_revisions: Mapping[str, str] | None = None,
) -> str:
    """Return a stable content fingerprint independent of observation time/CRM stage."""

    # Revisions are evidence metadata.  A source may advance its own revision while
    # yielding the same business content, which must not create another snapshot.
    del source_revisions

    def normalized_lines(lines: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        result = [
            {
                "product_ref": _clean(item.get("product_ref")),
                "product_code": _clean(item.get("product_code")),
                "quantity": str(_quantity(item.get("quantity"))),
                "rtu_external_id": _clean(item.get("rtu_external_id")),
                "basket_item_id": item.get("basket_item_id"),
            }
            for item in lines
        ]
        return sorted(
            result,
            key=lambda item: (
                item["product_ref"],
                item["rtu_external_id"],
                item["quantity"],
                str(item["basket_item_id"] or ""),
            ),
        )

    normalized_rtus = [
        {
            "external_id": _clean(raw.get("external_id")),
            "posted": bool(raw.get("posted")),
            "assembled_at": _json_ready(raw.get("assembled_at")),
            "cancelled_at": _json_ready(raw.get("cancelled_at")),
            "source_revision": _clean(raw.get("source_revision")),
            "items": normalized_lines(raw.get("items") or []),
        }
        for raw in rtus
    ]
    normalized_rtus.sort(key=lambda item: item["external_id"])
    normalized_shipments = [
        {
            "shipment_key": _clean(raw.get("shipment_key"))
            or (
                f"bitrix:{raw.get('bitrix_shipment_id')}"
                if raw.get("bitrix_shipment_id") not in (None, "")
                else ""
            ),
            "bitrix_shipment_id": raw.get("bitrix_shipment_id"),
            "delivery_service_id": raw.get("delivery_service_id"),
            "carrier": _clean(raw.get("carrier")).lower(),
            "tracking_number": _clean(raw.get("tracking_number")),
            "status": _clean(raw.get("status")).lower() or STATUS_PLANNED,
            "dispatched_at": _json_ready(raw.get("dispatched_at")),
            "delivered_at": _json_ready(raw.get("delivered_at")),
            "returned_at": _json_ready(raw.get("returned_at")),
            "source_revision": _clean(raw.get("source_revision")),
            "items": normalized_lines(raw.get("items") or []),
        }
        for raw in shipments
    ]
    normalized_shipments.sort(key=lambda item: item["shipment_key"])
    return _fingerprint(
        {
            "site_order_number": _clean(site_order_number),
            "delivery_kind": _clean(delivery_kind).lower() or DELIVERY_UNKNOWN,
            "expected_items": normalized_lines(expected_items),
            "rtus": normalized_rtus,
            "shipments": normalized_shipments,
        }
    )


def validate_rtu_allocations(
    *,
    rtus: list[Mapping[str, Any]],
    shipments: list[Mapping[str, Any]],
) -> str | None:
    valid_rtus: dict[str, dict[str, Decimal]] = {}
    for raw in rtus:
        if not (
            bool(raw.get("posted")) and raw.get("assembled_at") and not raw.get("cancelled_at")
        ):
            continue
        external_id = _clean(raw.get("external_id"))
        if external_id:
            valid_rtus[external_id] = _aggregate_lines(raw.get("items") or [])

    allocated: defaultdict[tuple[str, str], Decimal] = defaultdict(Decimal)
    for shipment in shipments:
        status = _clean(shipment.get("status")).lower() or STATUS_PLANNED
        if status not in {STATUS_READY, STATUS_DISPATCHED, STATUS_DELIVERED}:
            continue
        for item in shipment.get("items") or []:
            rtu_external_id = _clean(item.get("rtu_external_id"))
            product_ref = _clean(item.get("product_ref"))
            require_ref = status in {STATUS_DISPATCHED, STATUS_DELIVERED} or len(valid_rtus) > 1
            if require_ref and not rtu_external_id:
                return "shipment_rtu_allocation_missing"
            if not rtu_external_id:
                continue
            if rtu_external_id not in valid_rtus:
                return "shipment_rtu_not_found_or_inactive"
            if product_ref not in valid_rtus[rtu_external_id]:
                return "shipment_rtu_product_mismatch"
            allocated[(rtu_external_id, product_ref)] += _quantity(item.get("quantity"))
    for (rtu_external_id, product_ref), quantity in allocated.items():
        if quantity - valid_rtus[rtu_external_id][product_ref] > QUANTITY_TOLERANCE:
            return "shipment_rtu_quantity_excess"
    return None


def _replace_rtu_items(rtu: SiteOrderRtu, items: list[Mapping[str, Any]]) -> None:
    grouped: dict[str, dict[str, Any]] = {}
    for item in items:
        product_ref = _clean(item.get("product_ref"))
        quantity = _quantity(item.get("quantity"))
        current = grouped.setdefault(
            product_ref,
            {
                "quantity": Decimal("0"),
                "product_code": _clean(item.get("product_code")) or None,
                "payload": item.get("payload"),
            },
        )
        current["quantity"] += quantity
    existing = {item.product_ref: item for item in rtu.items}
    for product_ref, value in sorted(grouped.items()):
        item = existing.pop(product_ref, None)
        if item is None:
            item = SiteOrderRtuItem(product_ref=product_ref)
            rtu.items.append(item)
        item.product_code = value["product_code"]
        item.quantity = value["quantity"]
        item.payload = value["payload"]
    for item in existing.values():
        rtu.items.remove(item)


def _replace_shipment_items(shipment: SiteOrderShipment, items: list[Mapping[str, Any]]) -> None:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for item in items:
        product_ref = _clean(item.get("product_ref"))
        rtu_external_id = _clean(item.get("rtu_external_id"))
        key = (product_ref, rtu_external_id)
        current = grouped.setdefault(
            key,
            {
                "quantity": Decimal("0"),
                "product_code": _clean(item.get("product_code")) or None,
                "bitrix_shipment_item_id": item.get("bitrix_shipment_item_id"),
                "basket_item_id": item.get("basket_item_id"),
                "payload": item.get("payload"),
            },
        )
        current["quantity"] += _quantity(item.get("quantity"))
    existing = {(item.product_ref, item.rtu_external_id): item for item in shipment.items}
    for (product_ref, rtu_external_id), value in sorted(grouped.items()):
        item = existing.pop((product_ref, rtu_external_id), None)
        if item is None:
            item = SiteOrderShipmentItem(
                product_ref=product_ref,
                rtu_external_id=rtu_external_id,
            )
            shipment.items.append(item)
        item.product_code = value["product_code"]
        item.quantity = value["quantity"]
        item.bitrix_shipment_item_id = value["bitrix_shipment_item_id"]
        item.basket_item_id = value["basket_item_id"]
        item.payload = value["payload"]
    for item in existing.values():
        shipment.items.remove(item)


def _shipment_revision_fingerprint(raw: Mapping[str, Any]) -> str:
    items = [
        {
            "product_ref": _clean(item.get("product_ref")),
            "product_code": _clean(item.get("product_code")),
            "rtu_external_id": _clean(item.get("rtu_external_id")),
            "quantity": str(_quantity(item.get("quantity"))),
        }
        for item in list(raw.get("items") or [])
    ]
    items.sort(
        key=lambda item: (
            item["product_ref"],
            item["rtu_external_id"],
            item["quantity"],
        )
    )
    return _fingerprint(
        _json_ready(
            {
                "carrier": _clean(raw.get("carrier")).lower(),
                "tracking_number": _clean(raw.get("tracking_number")),
                "status": _clean(raw.get("status")).lower() or STATUS_PLANNED,
                "dispatched_at": raw.get("dispatched_at"),
                "delivered_at": raw.get("delivered_at"),
                "returned_at": raw.get("returned_at"),
                "items": items,
            }
        )
    )


def _enqueue_crm_fields(
    session: Session,
    *,
    deal_id: int,
    order_number: str,
    coverage: AssemblyCoverage,
    shipment_count: int,
    full_assembly_at: datetime,
) -> SiteOrderFulfillmentOutbox | None:
    fields = {
        SHIPMENT_COUNT_FIELD: shipment_count,
        FULL_ASSEMBLY_STATUS_FIELD: coverage.status,
    }
    if coverage.complete:
        fields[FULL_ASSEMBLY_FIELD] = full_assembly_at.isoformat()
    else:
        fields[FULL_ASSEMBLY_FIELD] = ""
    key = f"shipment-crm-fields:{order_number}:{_fingerprint(fields)}"
    if session.scalar(
        select(SiteOrderFulfillmentOutbox.id).where(
            SiteOrderFulfillmentOutbox.idempotency_key == key
        )
    ):
        return None
    row = SiteOrderFulfillmentOutbox(
        operation=OP_UPDATE_SHIPMENT_CRM_FIELDS,
        target_type="deal",
        target_id=str(deal_id),
        status="pending",
        available_at=datetime.now(),
        idempotency_key=key,
        payload={
            "site_order_number": order_number,
            "deal_id": deal_id,
            "fields": fields,
            "coverage_status": coverage.status,
        },
    )
    session.add(row)
    return row


def _enqueue_notifications(
    session: Session,
    *,
    case: SiteOrderExecutionCase,
    shipments: list[SiteOrderShipment],
    email_enabled: bool,
    sms_enabled: bool,
) -> int:
    if len(shipments) <= 1:
        return 0
    created = 0
    channels = [
        channel
        for channel, enabled in ((CHANNEL_EMAIL, email_enabled), (CHANNEL_SMS, sms_enabled))
        if enabled
    ]
    ordered_shipments = sorted(
        shipments,
        key=lambda item: (item.part_number or 2_147_483_647, item.shipment_key),
    )
    for shipment in ordered_shipments:
        if shipment.legacy_owned:
            continue
        if shipment.status not in {STATUS_DISPATCHED, STATUS_DELIVERED}:
            continue
        if not _clean(shipment.tracking_number):
            continue
        for channel in channels:
            if session.scalar(
                select(SiteOrderShipmentNotification.id).where(
                    SiteOrderShipmentNotification.shipment_id == shipment.id,
                    SiteOrderShipmentNotification.channel == channel,
                    SiteOrderShipmentNotification.event_type == EVENT_DISPATCHED,
                )
            ):
                continue
            key = (
                f"shipment-notification:{case.site_order_number}:{shipment.shipment_key}:"
                f"{shipment.revision}:{channel}:{EVENT_DISPATCHED}"
            )
            if session.scalar(
                select(SiteOrderShipmentNotification.id).where(
                    SiteOrderShipmentNotification.idempotency_key == key
                )
            ):
                continue
            notification = SiteOrderShipmentNotification(
                shipment=shipment,
                channel=channel,
                event_type=EVENT_DISPATCHED,
                shipment_revision=shipment.revision,
                status="pending",
                idempotency_key=key,
                payload={
                    "site_order_number": case.site_order_number,
                    "deal_id": case.bitrix_deal_id,
                    "tracking_number": shipment.tracking_number,
                    "part_number": shipment.part_number,
                    "part_count": len(ordered_shipments),
                    "items": [
                        {
                            "product_ref": item.product_ref,
                            "product_code": item.product_code,
                            "quantity": str(item.quantity),
                        }
                        for item in shipment.items
                    ],
                },
            )
            session.add(notification)
            session.flush()
            session.add(
                SiteOrderFulfillmentOutbox(
                    operation=OP_START_SHIPMENT_NOTIFICATION,
                    target_type="shipment_notification",
                    target_id=str(notification.id),
                    status="pending",
                    available_at=datetime.now(),
                    idempotency_key=key,
                    payload={**(notification.payload or {}), "channel": channel},
                )
            )
            created += 1
    return created


def _enqueue_gateway_operations(
    session: Session,
    *,
    case: SiteOrderExecutionCase,
    bitrix_order_id: int | None,
    shipments: list[Mapping[str, Any]],
    snapshot_id: str,
    source_revisions: Mapping[str, str] | None,
) -> int:
    created = 0
    for raw in shipments:
        shipment_key = _clean(raw.get("shipment_key")) or (
            f"bitrix:{raw.get('bitrix_shipment_id')}"
            if raw.get("bitrix_shipment_id") not in (None, "")
            else ""
        )
        bitrix_shipment_id = raw.get("bitrix_shipment_id")
        action: str | None = None
        if bitrix_shipment_id in (None, ""):
            if not bool(raw.get("explicit_split_confirmed")):
                continue
            if bitrix_order_id is None:
                raise ValueError("bitrix_order_id_required_for_explicit_split")
            action = "ensure"
        elif bool(raw.get("tracking_update_confirmed")) and _clean(raw.get("tracking_number")):
            action = "update_tracking"
        if action is None:
            continue
        source_order_revision = _clean((source_revisions or {}).get("bitrix_sale"))
        if not source_order_revision:
            raise ValueError("bitrix_order_revision_required_for_gateway")
        payload = _json_ready(
            {
                "action": action,
                "snapshot_id": snapshot_id,
                "site_order_number": case.site_order_number,
                "deal_id": case.bitrix_deal_id,
                "order_id": bitrix_order_id,
                "source_order_revision": source_order_revision,
                "shipment_key": shipment_key,
                "bitrix_shipment_id": bitrix_shipment_id,
                "delivery_service_id": raw.get("delivery_service_id"),
                "tracking_number": raw.get("tracking_number"),
                "items": list(raw.get("items") or []),
            }
        )
        key = f"shipment-gateway:{case.site_order_number}:{shipment_key}:{_fingerprint(payload)}"
        if session.scalar(
            select(SiteOrderFulfillmentOutbox.id).where(
                SiteOrderFulfillmentOutbox.idempotency_key == key
            )
        ):
            continue
        session.add(
            SiteOrderFulfillmentOutbox(
                operation=OP_APPLY_SHIPMENT_GATEWAY,
                target_type="shipment_gateway",
                target_id=shipment_key,
                status="pending",
                available_at=datetime.now(),
                idempotency_key=key,
                payload=payload,
            )
        )
        created += 1
    return created


def sync_order_shipments(
    session: Session,
    *,
    site_order_number: str,
    bitrix_deal_id: int,
    current_stage: str | None,
    expected_items: list[Mapping[str, Any]],
    rtus: list[Mapping[str, Any]],
    shipments: list[Mapping[str, Any]],
    event_at: datetime,
    persist: bool,
    enqueue_crm_fields: bool,
    enqueue_notifications: bool,
    email_enabled: bool,
    sms_enabled: bool,
    snapshot_id: str | None = None,
    delivery_kind: str = DELIVERY_UNKNOWN,
    observed_at: datetime | None = None,
    source_revisions: Mapping[str, str] | None = None,
    bitrix_order_id: int | None = None,
    enqueue_gateway: bool = False,
) -> ShipmentSyncResult:
    computed_snapshot_id = build_snapshot_id(
        site_order_number=site_order_number,
        delivery_kind=delivery_kind,
        expected_items=expected_items,
        rtus=rtus,
        shipments=shipments,
        source_revisions=source_revisions,
    )
    if snapshot_id is not None and snapshot_id != computed_snapshot_id:
        raise ValueError("snapshot_id_mismatch")
    snapshot_id = computed_snapshot_id
    assembled_lines = [
        item
        for rtu in rtus
        if bool(rtu.get("posted")) and rtu.get("assembled_at") and not rtu.get("cancelled_at")
        for item in list(rtu.get("items") or [])
    ]
    coverage = evaluate_assembly_coverage(expected_items, assembled_lines)
    decision = derive_shipment_stage(
        current_stage=current_stage,
        coverage=coverage,
        expected_lines=expected_items,
        shipments=shipments,
        delivery_kind=delivery_kind,
    )
    allocation_error = validate_rtu_allocations(rtus=rtus, shipments=shipments)
    if allocation_error:
        decision = ShipmentStageDecision("manual_review", None, allocation_error)
    conflict = decision.action == "manual_review"
    payload = _json_ready(
        {
            "site_order_number": site_order_number,
            "bitrix_deal_id": bitrix_deal_id,
            "current_stage": current_stage,
            "event_at": event_at.isoformat(),
            "observed_at": (observed_at or event_at).isoformat(),
            "snapshot_id": snapshot_id,
            "delivery_kind": delivery_kind,
            "source_revisions": dict(source_revisions or {}),
            "coverage": asdict(coverage),
            "decision": asdict(decision),
            "expected_items": expected_items,
            "rtus": rtus,
            "shipments": shipments,
        }
    )
    fingerprint = snapshot_id
    if not persist:
        return ShipmentSyncResult(
            snapshot_id=snapshot_id,
            site_order_number=site_order_number,
            coverage_status=coverage.status,
            full_assembly=coverage.complete,
            shipment_count=len(shipments),
            target_stage=decision.target_stage,
            action=decision.action,
            reason=decision.reason,
            conflict=conflict,
        )

    event = fulfillment.upsert_execution_event(
        session,
        site_order_number=site_order_number,
        event_type="shipment_snapshot_reconciled",
        event_at=event_at,
        source="bitrix_sale",
        source_ref=f"shipment-snapshot:{site_order_number}:{snapshot_id}",
        confidence="strong" if not conflict else "medium",
        raw_message_id=None,
        payload=payload,
    )
    case = session.scalar(
        select(SiteOrderExecutionCase).where(
            SiteOrderExecutionCase.site_order_number == site_order_number
        )
    )
    if case is None:
        raise RuntimeError("execution_case_not_created")
    previous_control = (
        (case.payload or {}).get("shipment_control") if isinstance(case.payload, dict) else {}
    )
    previous_shipment_count = int((previous_control or {}).get("shipment_count") or 0)
    case.bitrix_deal_id = bitrix_deal_id
    case.current_crm_stage = current_stage
    if event is not None:
        case.last_evidence_event_id = event.id
        case.current_derived_status = "shipment_snapshot_reconciled"
        case.confidence = "strong" if not conflict else "medium"
    case.payload = {
        **(case.payload if isinstance(case.payload, dict) else {}),
        "shipment_control": {
            "fingerprint": fingerprint,
            "snapshot_id": snapshot_id,
            "observed_at": (observed_at or event_at).isoformat(),
            "delivery_kind": delivery_kind,
            "source_revisions": dict(source_revisions or {}),
            "coverage_status": coverage.status,
            "expected_items": expected_items,
            "shipment_count": len(shipments),
        },
    }
    case.updated_at = datetime.now()

    existing_rtus = {row.external_id: row for row in case.rtus}
    seen_rtu_ids: set[str] = set()
    for raw in rtus:
        external_id = _clean(raw.get("external_id"))
        if not external_id:
            raise ValueError("missing_rtu_external_id")
        seen_rtu_ids.add(external_id)
        rtu = existing_rtus.get(external_id)
        if rtu is None:
            rtu = SiteOrderRtu(case=case, external_id=external_id)
            session.add(rtu)
        rtu.number = _clean(raw.get("number")) or None
        rtu.posted = bool(raw.get("posted"))
        rtu.assembled_at = raw.get("assembled_at")
        rtu.cancelled_at = raw.get("cancelled_at")
        rtu.active = True
        rtu.last_seen_snapshot_id = snapshot_id
        rtu.retired_at = None
        rtu.source_revision = _clean(raw.get("source_revision")) or None
        rtu.payload = raw.get("payload")
        _replace_rtu_items(rtu, list(raw.get("items") or []))
    for external_id, rtu in existing_rtus.items():
        if external_id not in seen_rtu_ids and rtu.active:
            rtu.active = False
            rtu.retired_at = observed_at or event_at

    existing_shipments = {row.shipment_key: row for row in case.shipments}
    seen_shipment_keys: set[str] = set()
    next_part_number = max(
        (row.part_number or 0 for row in existing_shipments.values()),
        default=0,
    )
    persisted_shipments: list[SiteOrderShipment] = []
    for raw in sorted(
        shipments,
        key=lambda item: _clean(item.get("shipment_key"))
        or f"bitrix:{item.get('bitrix_shipment_id') or ''}",
    ):
        bitrix_id = raw.get("bitrix_shipment_id")
        shipment_key = _clean(raw.get("shipment_key")) or (
            f"bitrix:{bitrix_id}" if bitrix_id not in (None, "") else ""
        )
        if not shipment_key:
            raise ValueError("missing_shipment_key")
        seen_shipment_keys.add(shipment_key)
        shipment = existing_shipments.get(shipment_key)
        incoming_fingerprint = _shipment_revision_fingerprint(raw)
        if shipment is None:
            shipment = SiteOrderShipment(case=case, shipment_key=shipment_key)
            session.add(shipment)
            next_part_number += 1
            shipment.part_number = next_part_number
        elif (shipment.payload or {}).get("fingerprint") != incoming_fingerprint:
            shipment.revision += 1
        shipment.bitrix_shipment_id = int(bitrix_id) if bitrix_id not in (None, "") else None
        shipment.carrier = _clean(raw.get("carrier")) or None
        shipment.tracking_number = _clean(raw.get("tracking_number")) or None
        shipment.status = _clean(raw.get("status")).lower() or STATUS_PLANNED
        shipment.dispatched_at = raw.get("dispatched_at")
        shipment.delivered_at = raw.get("delivered_at")
        shipment.returned_at = raw.get("returned_at")
        shipment.active = True
        shipment.last_seen_snapshot_id = snapshot_id
        shipment.retired_at = None
        shipment.source_revision = _clean(raw.get("source_revision")) or None
        if shipment.part_number is None:
            next_part_number += 1
            shipment.part_number = next_part_number
        if (
            len(shipments) > 1
            and previous_shipment_count == 1
            and shipment.id is not None
            and shipment.status in {STATUS_DISPATCHED, STATUS_DELIVERED}
        ):
            shipment.legacy_owned = True
        shipment.payload = {**(raw.get("payload") or {}), "fingerprint": incoming_fingerprint}
        _replace_shipment_items(shipment, list(raw.get("items") or []))
        persisted_shipments.append(shipment)
    for shipment_key, shipment in existing_shipments.items():
        if shipment_key not in seen_shipment_keys and shipment.active:
            shipment.active = False
            shipment.retired_at = observed_at or event_at
    session.flush()

    stage_outbox_id: int | None = None
    if event is not None and decision.action == "update_stage" and decision.target_stage:
        stage_row = SiteOrderStageOutbox(
            case_id=case.id,
            event_id=event.id,
            idempotency_key=(
                f"shipment-stage:{site_order_number}:{fingerprint}:{decision.target_stage}"
            ),
            site_order_number=site_order_number,
            bitrix_deal_id=bitrix_deal_id,
            source_event_type="shipment_snapshot_reconciled",
            target_stage=decision.target_stage,
            payload={
                "pipeline": "shipment_reconciliation",
                "evidence_fingerprint": fingerprint,
                "coverage_status": coverage.status,
                "event_at": event_at.isoformat(),
                "decision": asdict(decision),
            },
        )
        session.add(stage_row)
        session.flush()
        stage_outbox_id = stage_row.id
    if conflict and event is not None:
        session.add(
            LogisticsManualReview(
                review_type="site_order_shipment_conflict",
                source_document_type="site_order",
                source_external_id=site_order_number,
                reason=decision.reason,
                payload={"event_id": event.id, "evidence_fingerprint": fingerprint},
            )
        )
    if enqueue_crm_fields:
        full_assembly_at = max(
            (
                assembled_at
                for raw in rtus
                if bool(raw.get("posted"))
                and not raw.get("cancelled_at")
                and (assembled_at := raw.get("assembled_at")) is not None
            ),
            default=event_at,
        )
        _enqueue_crm_fields(
            session,
            deal_id=bitrix_deal_id,
            order_number=site_order_number,
            coverage=coverage,
            shipment_count=len(shipments),
            full_assembly_at=full_assembly_at,
        )
    notification_count = (
        _enqueue_notifications(
            session,
            case=case,
            shipments=persisted_shipments,
            email_enabled=email_enabled,
            sms_enabled=sms_enabled,
        )
        if enqueue_notifications
        else 0
    )
    gateway_operation_count = (
        _enqueue_gateway_operations(
            session,
            case=case,
            bitrix_order_id=bitrix_order_id,
            shipments=shipments,
            snapshot_id=snapshot_id,
            source_revisions=source_revisions,
        )
        if enqueue_gateway
        else 0
    )
    session.flush()
    return ShipmentSyncResult(
        snapshot_id=snapshot_id,
        site_order_number=site_order_number,
        coverage_status=coverage.status,
        full_assembly=coverage.complete,
        shipment_count=len(shipments),
        target_stage=decision.target_stage,
        action=decision.action,
        reason=decision.reason,
        event_id=event.id if event is not None else None,
        stage_outbox_id=stage_outbox_id,
        notification_count=notification_count,
        gateway_operation_count=gateway_operation_count,
        conflict=conflict,
    )


def update_notification_status(
    session: Session,
    *,
    idempotency_key: str,
    status: str,
    occurred_at: datetime,
    external_ref: str | None = None,
    error: str | None = None,
) -> ShipmentNotificationStatusResult:
    notification = session.scalar(
        select(SiteOrderShipmentNotification).where(
            SiteOrderShipmentNotification.idempotency_key == idempotency_key
        )
    )
    if notification is None:
        raise ValueError("shipment_notification_not_found")
    known_statuses = {"pending", "submitted", "sent", "delivered", "failed"}
    if status not in known_statuses:
        raise ValueError("shipment_notification_status_unknown")
    current = notification.status if notification.status in known_statuses else "pending"
    allowed = {
        "pending": {"pending", "submitted", "sent", "delivered", "failed"},
        "submitted": {"submitted", "sent", "delivered", "failed"},
        "sent": {"sent", "delivered"},
        "delivered": {"delivered"},
        # A failed workflow may be retried with the same idempotency key. A later
        # success is authoritative; a late failure cannot regress sent/delivered.
        "failed": {"failed", "submitted", "sent", "delivered"},
    }
    if status not in allowed[current]:
        return ShipmentNotificationStatusResult(idempotency_key, current, False)
    changed = current != status
    notification.status = status
    if external_ref:
        notification.external_ref = external_ref
    if status == "submitted":
        notification.submitted_at = notification.submitted_at or occurred_at
    elif status == "sent":
        notification.submitted_at = notification.submitted_at or occurred_at
        notification.sent_at = notification.sent_at or occurred_at
    elif status == "delivered":
        notification.submitted_at = notification.submitted_at or occurred_at
        notification.sent_at = notification.sent_at or occurred_at
        notification.delivered_at = notification.delivered_at or occurred_at
    elif status == "failed":
        notification.failed_at = notification.failed_at or occurred_at
        notification.last_error = _clean(error)[:1000] or "workflow_failed"
    if status != "failed":
        notification.last_error = None
    return ShipmentNotificationStatusResult(idempotency_key, notification.status, changed)
