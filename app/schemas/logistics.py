from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, model_validator


class LogisticsPhotoInput(BaseModel):
    telegram_file_id: str = Field(min_length=1, max_length=255)
    comment: str | None = Field(default=None, max_length=1000)


class LogisticsWarehouseSyncItem(BaseModel):
    external_id: str
    name: str
    kind: str = "store"
    payload: dict | None = None
    is_active: bool = True


class LogisticsDriverSyncItem(BaseModel):
    external_id: str | None = None
    full_name: str
    phone: str | None = None
    is_active: bool = True


class LogisticsUserSyncItem(BaseModel):
    external_id: str | None = None
    telegram_user_id: int | None = None
    bitrix_user_id: str | None = None
    username: str | None = None
    full_name: str
    role: str
    default_warehouse_external_id: str | None = None
    is_active: bool = True


class LogisticsTransferSyncItem(BaseModel):
    source_document_type: str = "transfer"
    external_id: str
    document_number: str
    document_date: datetime
    source_warehouse_external_id: str
    target_warehouse_external_id: str
    document_target_warehouse_external_id: str | None = None
    final_recipient_name: str | None = None
    barcode: str | None = None
    lookup_code: str | None = None
    origin_order_external_id: str | None = None
    site_order_number: str | None = None
    status: str | None = None
    onec_deleted: bool = False
    payload: dict | None = None


class LogisticsUnitSyncItem(LogisticsTransferSyncItem):
    pass


class LogisticsSyncResponse(BaseModel):
    created: int
    updated: int


class LogisticsTelegramAuthRequest(BaseModel):
    telegram_user_id: int
    username: str | None = None


class LogisticsUserProfile(BaseModel):
    id: int
    external_id: str | None = None
    telegram_user_id: int | None = None
    bitrix_user_id: str | None = None
    username: str | None = None
    full_name: str
    role: str
    default_warehouse_id: int | None = None
    default_warehouse_name: str | None = None


class LogisticsDraftCreateRequest(BaseModel):
    actor_user_id: int
    warehouse_id: int
    driver_id: int | None = None
    route_run_id: int | None = None
    default_dropoff_warehouse_id: int | None = None
    comment: str | None = Field(default=None, max_length=1000)


class LogisticsDraftScanRequest(BaseModel):
    actor_user_id: int
    barcode: str | None = Field(default=None, max_length=255)
    lookup_code: str | None = Field(default=None, max_length=255)
    dropoff_warehouse_id: int | None = None

    @model_validator(mode="after")
    def require_lookup_value(self):
        if not self.barcode and not self.lookup_code:
            raise ValueError("barcode or lookup_code is required")
        return self


class LogisticsDraftConfirmRequest(BaseModel):
    actor_user_id: int
    comment: str | None = Field(default=None, max_length=1000)
    idempotency_key: str | None = Field(default=None, max_length=255)
    photos: list[LogisticsPhotoInput] = Field(default_factory=list, max_length=20)


class LogisticsDraftCancelRequest(BaseModel):
    actor_user_id: int
    reason: str | None = Field(default=None, max_length=1000)


class LogisticsDraftItemRemoveRequest(BaseModel):
    actor_user_id: int


class LogisticsDraftItemResponse(BaseModel):
    id: int
    transfer_id: int
    barcode: str
    lookup_code: str | None = None
    source_document_type: str = "transfer"
    document_number: str
    final_recipient_name: str | None = None
    dropoff_warehouse_id: int | None = None
    dropoff_warehouse_name: str | None = None
    scan_at: datetime


class LogisticsDraftResponse(BaseModel):
    id: int
    draft_type: str
    status: str
    warehouse_id: int
    driver_id: int | None = None
    route_run_id: int | None = None
    default_dropoff_warehouse_id: int | None = None
    cancelled_at: datetime | None = None
    cancelled_by_user_id: int | None = None
    cancel_reason: str | None = None
    item_count: int
    items: list[LogisticsDraftItemResponse]


class LogisticsConfirmResponse(BaseModel):
    draft_id: int
    status: str
    processed_count: int
    event_type: str


class LogisticsExpectedDeliveryResponse(BaseModel):
    transfer_id: int
    external_id: str
    source_document_type: str = "transfer"
    document_number: str
    barcode: str
    lookup_code: str | None = None
    site_order_number: str | None = None
    source_warehouse_name: str
    target_warehouse_name: str
    final_recipient_name: str | None = None
    driver_name: str | None = None
    dropoff_warehouse_name: str | None = None
    last_event_type: str
    last_event_at: datetime


class LogisticsMonitorResponse(BaseModel):
    transfer_id: int
    external_id: str
    source_document_type: str = "transfer"
    document_number: str
    document_date: datetime
    barcode: str
    lookup_code: str | None = None
    site_order_number: str | None = None
    source_warehouse_name: str
    target_warehouse_name: str
    final_recipient_name: str | None = None
    status: str
    current_warehouse_name: str | None = None
    dropoff_warehouse_name: str | None = None
    driver_name: str | None = None
    last_event_type: str
    last_event_at: datetime
    last_user_name: str | None = None
    route_run_id: int | None = None
    route_name: str | None = None
    manual_review_count: int = 0


class LogisticsEventActionRequest(BaseModel):
    actor_user_id: int
    warehouse_id: int | None = None
    comment: str | None = Field(default=None, max_length=1000)
    idempotency_key: str | None = Field(default=None, max_length=255)
    photos: list[LogisticsPhotoInput] = Field(default_factory=list, max_length=20)


class LogisticsEventPhotoResponse(BaseModel):
    telegram_file_id: str
    comment: str | None = None


class LogisticsHistoryEventResponse(BaseModel):
    id: int
    event_type: str
    event_at: datetime
    warehouse_name: str | None = None
    dropoff_warehouse_name: str | None = None
    driver_name: str | None = None
    user_name: str | None = None
    comment: str | None = None
    source: str
    photos: list[LogisticsEventPhotoResponse]


class LogisticsUnitLookupResponse(BaseModel):
    transfer_id: int
    source_document_type: str
    external_id: str
    document_number: str
    barcode: str
    lookup_code: str | None = None
    site_order_number: str | None = None
    status: str
    current_warehouse_id: int | None = None
    dropoff_warehouse_id: int | None = None
    target_warehouse_id: int
    document_target_warehouse_id: int | None = None


class LogisticsWarehouseResponse(BaseModel):
    id: int
    external_id: str
    name: str
    kind: str
    payload: dict | None = None
    is_active: bool


class LogisticsDriverResponse(BaseModel):
    id: int
    external_id: str | None = None
    full_name: str
    phone: str | None = None
    is_active: bool


class LogisticsRouteRunItemRequest(BaseModel):
    transfer_id: int | None = None
    lookup_code: str | None = None
    dropoff_warehouse_id: int | None = None
    leg_sequence: int | None = None
    status: str | None = None

    @model_validator(mode="after")
    def require_transfer_reference(self):
        if self.transfer_id is None and not self.lookup_code:
            raise ValueError("transfer_id or lookup_code is required")
        return self


class LogisticsRouteRunCreateRequest(BaseModel):
    external_id: str | None = None
    route_name: str
    planned_at: datetime | None = None
    driver_id: int | None = None
    status: str = "planned"
    payload: dict | None = None
    items: list[LogisticsRouteRunItemRequest] = Field(default_factory=list)


class LogisticsRouteRunItemResponse(BaseModel):
    id: int
    transfer_id: int
    source_document_type: str
    external_id: str
    document_number: str
    barcode: str
    lookup_code: str | None = None
    dropoff_warehouse_id: int | None = None
    dropoff_warehouse_name: str | None = None
    leg_sequence: int | None = None
    status: str
    completed_at: datetime | None = None


class LogisticsRouteRunResponse(BaseModel):
    id: int
    external_id: str | None = None
    route_name: str
    planned_at: datetime | None = None
    driver_id: int | None = None
    driver_name: str | None = None
    status: str
    payload: dict | None = None
    items: list[LogisticsRouteRunItemResponse]


class LogisticsManualReviewResponse(BaseModel):
    id: int
    review_type: str
    status: str
    source_document_type: str | None = None
    source_external_id: str | None = None
    transfer_id: int | None = None
    document_number: str | None = None
    reason: str
    payload: dict | None = None
    resolved_by_user_id: int | None = None
    resolved_by_user_name: str | None = None
    resolved_at: datetime | None = None
    created_at: datetime


class LogisticsExternalCarrierHandoffRequest(BaseModel):
    actor_user_id: int
    carrier_name: str
    tracking_number: str | None = None
    carrier_terminal: str | None = None
    comment: str | None = Field(default=None, max_length=1000)
    idempotency_key: str | None = Field(default=None, max_length=255)


class LogisticsExternalCarrierAcceptRequest(BaseModel):
    actor_user_id: int
    warehouse_id: int
    comment: str | None = Field(default=None, max_length=1000)
    idempotency_key: str | None = Field(default=None, max_length=255)


class LogisticsManualReadyOverrideRequest(BaseModel):
    actor_user_id: int
    source_document_type: str
    external_id: str
    warehouse_id: int
    reason: str
    lookup_code: str | None = None
    site_order_number: str | None = None


class LogisticsTransferAssistantProduct(BaseModel):
    ref: str | None = None
    code: str | None = None
    name: str | None = None


class LogisticsTransferAssistantWarehouse(BaseModel):
    ref: str | None = None
    code: str | None = None
    name: str | None = None


class LogisticsTransferAssistantOrder(BaseModel):
    ref: str | None = None
    number: str | None = None
    site_order_number: str | None = None


class LogisticsTransferAssistantSourceDocument(BaseModel):
    type: str | None = None
    ref: str | None = None
    number: str | None = None


class LogisticsTransferAssistantCandidateResponse(BaseModel):
    product: LogisticsTransferAssistantProduct
    warehouse: LogisticsTransferAssistantWarehouse
    order: LogisticsTransferAssistantOrder | None = None
    source_document: LogisticsTransferAssistantSourceDocument | None = None
    quantity: Decimal
    status: str
    reason: str
    onec_document_keys: dict[str, str] = Field(default_factory=dict)
    fact_date: datetime | None = None
    data_source: str
    measures: dict[str, Decimal] = Field(default_factory=dict)
    pickup_deadline: datetime | None = None
    pickup_deadline_source: str | None = None
