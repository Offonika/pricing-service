from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field


class OrderFulfillmentMentionResponse(BaseModel):
    site_order_number: str
    event_type: str
    confidence: str
    evidence_text: str | None = None
    payload: dict[str, Any] | None = None


class BitrixChatMessageIngestRequest(BaseModel):
    chat_code: str
    dialog_id: str = "chat733"
    chat_id: int
    message_id: int
    message_at: datetime | None = None
    author_id: str | None = None
    text: str | None = None
    payload: dict[str, Any] | None = None
    ocr_payloads: list[dict[str, Any]] = Field(default_factory=list)
    dry_run: bool = False


class BitrixChatMessageIngestResponse(BaseModel):
    message_id: int
    parse_status: str
    duplicate_message: bool = False
    mentions: list[OrderFulfillmentMentionResponse] = Field(default_factory=list)
    events_created: int = 0


class BitrixChatIngestResponse(BaseModel):
    chat_code: str
    dialog_id: str
    messages: int = 0
    mentions: int = 0
    events: int = 0
    duplicates: int = 0
    ocr_images: int = 0


class OrderFulfillmentRecommendationItem(BaseModel):
    site_order_number: str
    bitrix_deal_id: int | None = None
    current_stage: str | None = None
    recommended_stage: str | None = None
    derived_status: str
    confidence: str | None = None
    action: str
    evidence_event_id: int | None = None


class OrderFulfillmentRecommendationsResponse(BaseModel):
    items: list[OrderFulfillmentRecommendationItem] = Field(default_factory=list)


class OrderFulfillmentReviewItem(BaseModel):
    site_order_number: str
    bitrix_deal_id: int | None = None
    crm_stage: str | None = None
    crm_delivery: str | None = None
    crm_payment_status: str | None = None
    onec_raw_delivery: str | None = None
    onec_order_date: str | None = None
    onec_courier: str | None = None
    onec_delivery_cost: str | None = None
    chat_event: str
    event_confidence: str | None = None
    evidence_redacted: str | None = None
    recommended_stage: str | None = None
    action: str
    manual_review_reason: str | None = None


class OrderFulfillmentReviewResponse(BaseModel):
    items: list[OrderFulfillmentReviewItem] = Field(default_factory=list)


class DeliveryMethodReportItem(BaseModel):
    raw_delivery_method: str
    count: int
    status: str
    note: str | None = None


class DeliveryMethodReportResponse(BaseModel):
    items: list[DeliveryMethodReportItem] = Field(default_factory=list)


class OnecExecutionEventIngestRequest(BaseModel):
    signal: Literal["assembled", "issued"]
    event_at: datetime
    site_order_number: str = Field(min_length=1, max_length=32)
    onec_order_number: str | None = Field(default=None, max_length=64)
    rtu_external_id: str | None = Field(default=None, max_length=64)
    rtu_number: str | None = Field(default=None, max_length=64)
    rtu_date: datetime | None = None
    is_posted: bool = True
    document_amount: Decimal | None = None
    dry_run: bool = False


class OnecExecutionEventIngestResponse(BaseModel):
    accepted: bool
    duplicate: bool = False
    event_id: int | None = None
    source_ref: str
    reconciliation_queued: bool = False


class ShipmentLineInput(BaseModel):
    product_ref: str = Field(min_length=1, max_length=64)
    product_code: str | None = None
    quantity: Decimal = Field(gt=0)
    rtu_external_id: str | None = None
    bitrix_shipment_item_id: int | None = Field(default=None, ge=1)
    basket_item_id: int | None = Field(default=None, ge=1)
    payload: dict[str, Any] | None = None


class RtuSnapshotInput(BaseModel):
    external_id: str
    number: str | None = None
    posted: bool = False
    assembled_at: datetime | None = None
    cancelled_at: datetime | None = None
    source_revision: str | None = Field(default=None, max_length=128)
    items: list[ShipmentLineInput] = Field(default_factory=list)
    payload: dict[str, Any] | None = None


class ShipmentSnapshotInput(BaseModel):
    shipment_key: str | None = None
    bitrix_shipment_id: int | None = Field(default=None, ge=1)
    delivery_service_id: int | None = Field(default=None, ge=1)
    carrier: str | None = None
    tracking_number: str | None = None
    status: Literal[
        "planned",
        "ready",
        "dispatched",
        "delivered",
        "returned",
        "conflict",
    ] = "planned"
    dispatched_at: datetime | None = None
    delivered_at: datetime | None = None
    returned_at: datetime | None = None
    source_revision: str | None = Field(default=None, max_length=128)
    explicit_split_confirmed: bool = False
    tracking_update_confirmed: bool = False
    items: list[ShipmentLineInput] = Field(default_factory=list)
    payload: dict[str, Any] | None = None


class OrderShipmentsSyncRequest(BaseModel):
    snapshot_id: str | None = Field(default=None, min_length=64, max_length=64)
    site_order_number: str = Field(min_length=1, max_length=64)
    bitrix_deal_id: int = Field(ge=1)
    bitrix_order_id: int | None = Field(default=None, ge=1)
    current_stage: str | None = None
    delivery_kind: Literal["carrier", "internal_pickup", "unknown"] = "unknown"
    event_at: datetime
    observed_at: datetime | None = None
    source_revisions: dict[str, str] = Field(default_factory=dict)
    expected_items: list[ShipmentLineInput]
    rtus: list[RtuSnapshotInput] = Field(default_factory=list)
    shipments: list[ShipmentSnapshotInput] = Field(default_factory=list)
    dry_run: bool = True


class OrderShipmentsSyncResponse(BaseModel):
    snapshot_id: str
    site_order_number: str
    coverage_status: str
    full_assembly: bool
    shipment_count: int
    target_stage: str | None = None
    action: str
    reason: str
    event_id: int | None = None
    stage_outbox_id: int | None = None
    notification_count: int = 0
    gateway_operation_count: int = 0
    conflict: bool = False


class ShipmentNotificationStatusRequest(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=255)
    status: Literal["submitted", "sent", "delivered", "failed"]
    occurred_at: datetime
    external_ref: str | None = Field(default=None, max_length=255)
    error: str | None = Field(default=None, max_length=1000)


class ShipmentNotificationStatusResponse(BaseModel):
    idempotency_key: str
    status: str
    changed: bool
