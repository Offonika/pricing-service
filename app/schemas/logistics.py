from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class LogisticsPhotoInput(BaseModel):
    telegram_file_id: str
    comment: str | None = None


class LogisticsWarehouseSyncItem(BaseModel):
    external_id: str
    name: str
    kind: str = "store"
    is_active: bool = True


class LogisticsDriverSyncItem(BaseModel):
    external_id: str | None = None
    full_name: str
    phone: str | None = None
    is_active: bool = True


class LogisticsUserSyncItem(BaseModel):
    external_id: str | None = None
    telegram_user_id: int | None = None
    username: str | None = None
    full_name: str
    role: str
    default_warehouse_external_id: str | None = None
    is_active: bool = True


class LogisticsTransferSyncItem(BaseModel):
    external_id: str
    document_number: str
    document_date: datetime
    source_warehouse_external_id: str
    target_warehouse_external_id: str
    final_recipient_name: str | None = None
    barcode: str
    status: str | None = None
    onec_deleted: bool = False
    payload: dict | None = None


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
    username: str | None = None
    full_name: str
    role: str
    default_warehouse_id: int | None = None
    default_warehouse_name: str | None = None


class LogisticsDraftCreateRequest(BaseModel):
    actor_user_id: int
    warehouse_id: int
    driver_id: int | None = None
    default_dropoff_warehouse_id: int | None = None
    comment: str | None = None


class LogisticsDraftScanRequest(BaseModel):
    actor_user_id: int
    barcode: str
    dropoff_warehouse_id: int | None = None


class LogisticsDraftConfirmRequest(BaseModel):
    actor_user_id: int
    comment: str | None = None
    idempotency_key: str | None = None
    photos: list[LogisticsPhotoInput] = Field(default_factory=list)


class LogisticsDraftItemResponse(BaseModel):
    id: int
    transfer_id: int
    barcode: str
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
    default_dropoff_warehouse_id: int | None = None
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
    document_number: str
    barcode: str
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
    document_number: str
    document_date: datetime
    barcode: str
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


class LogisticsEventActionRequest(BaseModel):
    actor_user_id: int
    warehouse_id: int | None = None
    comment: str | None = None
    idempotency_key: str | None = None
    photos: list[LogisticsPhotoInput] = Field(default_factory=list)


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
