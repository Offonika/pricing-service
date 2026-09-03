from __future__ import annotations

from datetime import datetime

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from app.schemas.customer_returns import CustomerReturnCarrier
from app.schemas.logistics import (
    LogisticsDraftResponse,
    LogisticsDriverResponse,
    LogisticsUserProfile,
    LogisticsWarehouseResponse,
)


class BitrixLogisticsSessionRequest(BaseModel):
    access_token: str = Field(min_length=1)
    domain: str = Field(min_length=1)
    member_id: str = Field(min_length=1)


class BitrixLogisticsSessionResponse(BaseModel):
    session_token: str
    token_type: str = "bearer"
    expires_at: datetime
    expires_in: int
    profile: LogisticsUserProfile


class BitrixLogisticsBootstrapResponse(BaseModel):
    profile: LogisticsUserProfile
    warehouses: list[LogisticsWarehouseResponse]
    drivers: list[LogisticsDriverResponse]
    capabilities: list[str]
    open_draft: LogisticsDraftResponse | None = None


class BitrixCustomerReturnCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    carrier: CustomerReturnCarrier
    tracking_number: str = Field(min_length=5, max_length=64)
    source_ref: str | None = Field(default=None, min_length=1, max_length=128)
    bitrix_case_id: str | None = Field(default=None, min_length=1, max_length=64)
    site_ticket_id: str | None = Field(default=None, min_length=1, max_length=64)
    onec_order_ref: str | None = Field(default=None, min_length=1, max_length=64)
    bitrix_deal_id: int | None = Field(default=None, ge=1)
    service_request_item_id: int | None = Field(
        default=None,
        ge=1,
        validation_alias=AliasChoices("serviceRequestItemId", "service_request_item_id"),
    )


class BitrixCustomerReturnDealLinkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bitrix_deal_id: int | None = Field(default=None, ge=1)


class BitrixCustomerReturnDealSearchItem(BaseModel):
    deal_id: int
    title: str
    order_ref: str | None = None
    stage_id: str | None = None
    stage_name: str | None = None
    closed: bool
    created_at: datetime | None = None
    contact_id: int | None = None
    contact_name: str | None = None
    company_id: int | None = None
    company_name: str | None = None
    responsible_user_id: int | None = None
    responsible_name: str | None = None


class BitrixCustomerReturnServiceRequestLinkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service_request_item_id: int | None = Field(
        default=None,
        ge=1,
        validation_alias=AliasChoices("serviceRequestItemId", "service_request_item_id"),
    )


class BitrixCustomerReturnServiceRequestSearchItem(BaseModel):
    item_id: int
    title: str
    stage_id: str | None = None
    stage_name: str | None = None
    closed: bool = False
    category_id: int | None = None
    deal_id: int | None = None
    order_ref: str | None = None
    responsible_user_id: int | None = None
    responsible_name: str | None = None
    site_ticket_id: str | None = None


class BitrixCustomerReturnExpertiseLinkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service_request_item_id: int | None = Field(
        default=None,
        ge=1,
        validation_alias=AliasChoices("serviceRequestItemId", "service_request_item_id"),
    )


class BitrixCustomerReturnExpertiseItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    external_id: str
    onec_expertise_number: str | None = None
    current_status: str
    linked_customer_order_number: str | None = None
    problem_summary: str | None = None
    service_request_item_id: int | None = None


class BitrixCustomerReturnPickupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)
    comment: str | None = Field(default=None, max_length=500)


class BitrixLogisticsManualReviewItem(BaseModel):
    id: int
    review_type: str
    source_document_type: str | None = None
    transfer_id: int | None = None
    document_number: str | None = None
    rtu_number: str | None = None
    onec_order_number: str | None = None
    site_order_number: str | None = None
    source_warehouse_name: str | None = None
    delivery_method: str | None = None
    created_at: datetime


class BitrixLogisticsManualReviewPage(BaseModel):
    items: list[BitrixLogisticsManualReviewItem]
    total: int
    limit: int
    offset: int
    counts: dict[str, int]


class BitrixLogisticsDraftCreateRequest(BaseModel):
    warehouse_id: int
    driver_id: int | None = None
    route_run_id: int | None = None
    comment: str | None = Field(default=None, max_length=1000)


class BitrixLogisticsDraftScanRequest(BaseModel):
    barcode: str | None = Field(default=None, max_length=255)
    lookup_code: str | None = Field(default=None, max_length=255)


class BitrixLogisticsDraftConfirmRequest(BaseModel):
    comment: str | None = Field(default=None, max_length=1000)
    idempotency_key: str | None = Field(default=None, max_length=255)


class BitrixLogisticsDraftCancelRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=1000)


class BitrixLogisticsFallbackLinkResponse(BaseModel):
    url: str
    expires_at: datetime


class BitrixLogisticsFallbackSessionRequest(BaseModel):
    token: str = Field(min_length=20)
