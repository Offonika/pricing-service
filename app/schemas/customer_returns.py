from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CustomerReturnCarrier = Literal["russian_post", "cdek"]
CustomerReturnStatus = Literal[
    "registered",
    "in_transit",
    "arrived_at_pickup_point",
    "picked_up",
    "onec_return_confirmed",
    "cancelled",
    "exception",
]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CustomerReturnCreateRequest(BaseModel):
    carrier: CustomerReturnCarrier
    tracking_number: str = Field(min_length=5, max_length=64)
    source: str = Field(default="manual", min_length=1, max_length=32)
    source_ref: str | None = Field(default=None, min_length=1, max_length=128)
    bitrix_case_id: str | None = Field(default=None, min_length=1, max_length=64)
    site_ticket_id: str | None = Field(default=None, min_length=1, max_length=64)
    onec_order_ref: str | None = Field(default=None, min_length=1, max_length=64)
    created_by_bitrix_user_id: str | None = Field(default=None, min_length=1, max_length=64)
    payload: dict | None = None


class CustomerReturnCarrierEventRequest(BaseModel):
    status_code: str = Field(min_length=1, max_length=128)
    status_text: str | None = Field(default=None, max_length=500)
    occurred_at: datetime = Field(default_factory=_utcnow)
    external_event_id: str | None = Field(default=None, min_length=1, max_length=128)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)
    storage_deadline_at: datetime | None = None
    payload: dict | None = None


class CustomerReturnPickupRequest(BaseModel):
    actor_bitrix_user_id: str = Field(min_length=1, max_length=64)
    occurred_at: datetime = Field(default_factory=_utcnow)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)
    comment: str | None = Field(default=None, max_length=500)


class CustomerReturnOneCConfirmationRequest(BaseModel):
    onec_return_ref: str = Field(min_length=1, max_length=64)
    occurred_at: datetime = Field(default_factory=_utcnow)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)


class CustomerReturnActionCompleteRequest(BaseModel):
    external_reference: str = Field(min_length=1, max_length=128)
    completed_at: datetime = Field(default_factory=_utcnow)


class CustomerReturnEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    event_type: str
    source: str
    normalized_status: str | None = None
    carrier_status_code: str | None = None
    carrier_status_text: str | None = None
    external_event_id: str | None = None
    actor_bitrix_user_id: str | None = None
    occurred_at: datetime
    payload: dict | None = None
    created_at: datetime


class CustomerReturnActionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    shipment_id: int
    action_type: str
    status: str
    due_at: datetime
    next_attempt_at: datetime | None = None
    leased_until: datetime | None = None
    external_reference: str | None = None
    attempt_count: int
    last_error: str | None = None
    completed_at: datetime | None = None
    payload: dict | None = None
    created_at: datetime
    updated_at: datetime


class CustomerReturnShipmentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    carrier: str
    tracking_number: str
    status: CustomerReturnStatus
    status_changed_at: datetime
    source: str
    source_ref: str | None = None
    bitrix_case_id: str | None = None
    site_ticket_id: str | None = None
    onec_order_ref: str | None = None
    bitrix_deal_id: int | None = None
    bitrix_deal_title: str | None = None
    bitrix_order_ref: str | None = None
    bitrix_deal_stage_id: str | None = None
    bitrix_deal_stage_name: str | None = None
    bitrix_deal_closed: bool | None = None
    bitrix_contact_id: int | None = None
    bitrix_contact_name: str | None = None
    bitrix_company_id: int | None = None
    bitrix_company_name: str | None = None
    bitrix_responsible_user_id: int | None = None
    bitrix_responsible_name: str | None = None
    bitrix_deal_linked_at: datetime | None = None
    bitrix_deal_linked_by_user_id: str | None = None
    onec_return_ref: str | None = None
    created_by_bitrix_user_id: str | None = None
    picked_up_by_bitrix_user_id: str | None = None
    carrier_last_status_code: str | None = None
    carrier_last_status_text: str | None = None
    carrier_last_event_at: datetime | None = None
    storage_deadline_at: datetime | None = None
    arrived_at: datetime | None = None
    picked_up_at: datetime | None = None
    onec_return_confirmed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class CustomerReturnDetailResponse(CustomerReturnShipmentResponse):
    events: list[CustomerReturnEventResponse]
    actions: list[CustomerReturnActionResponse]


class CustomerReturnRegistrationResponse(BaseModel):
    created: bool
    shipment: CustomerReturnDetailResponse


class CustomerReturnEventIngestResponse(BaseModel):
    event_created: bool
    shipment: CustomerReturnDetailResponse
