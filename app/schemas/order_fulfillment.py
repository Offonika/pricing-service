from __future__ import annotations

from datetime import datetime
from typing import Any

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


class AssemblyEventIngestResponse(BaseModel):
    accepted: bool = True
    event_key: str
    outbox_id: int
    status: str
    duplicate: bool = False


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
