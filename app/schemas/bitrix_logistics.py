from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.logistics import (
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


class BitrixLogisticsDraftCreateRequest(BaseModel):
    warehouse_id: int
    driver_id: int | None = None
    route_run_id: int | None = None
    default_dropoff_warehouse_id: int | None = None
    comment: str | None = None


class BitrixLogisticsDraftScanRequest(BaseModel):
    barcode: str | None = None
    lookup_code: str | None = None
    dropoff_warehouse_id: int | None = None


class BitrixLogisticsDraftConfirmRequest(BaseModel):
    comment: str | None = None
    idempotency_key: str | None = None


class BitrixLogisticsFallbackLinkResponse(BaseModel):
    url: str
    expires_at: datetime


class BitrixLogisticsFallbackSessionRequest(BaseModel):
    token: str = Field(min_length=20)
