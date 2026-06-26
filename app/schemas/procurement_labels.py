from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class ProcurementLabelsSessionRequest(BaseModel):
    access_token: str = Field(min_length=1)
    domain: str = Field(min_length=1)
    member_id: str = Field(min_length=1)


class ProcurementLabelsUser(BaseModel):
    user_id: str
    name: str | None = None


class ProcurementLabelsSessionResponse(BaseModel):
    session_token: str
    token_type: str = "bearer"
    expires_at: datetime
    expires_in: int
    user: ProcurementLabelsUser


class ProcurementLabelRow(BaseModel):
    line_no: int
    onec_item_code: str
    item_name: str
    sku: str
    barcode: str
    unit: str
    quantity: Decimal
    price: Decimal | None = None
    amount: Decimal | None = None
    certificate_id: str = ""
    certificate_status: str
    eac_allowed: bool
    status: str
    blockers: list[str] = Field(default_factory=list)


class ProcurementLabelOrderPreview(BaseModel):
    item_id: str
    entity_type_id: int
    onec_number: str
    title: str
    contour: str
    status: str
    ready: bool
    blocked: bool
    blockers: list[str] = Field(default_factory=list)
    rows: list[ProcurementLabelRow] = Field(default_factory=list)
    artifact_version: int | None = None
    zip_url: str | None = None
    disk_file_id: str | None = None


class ProcurementLabelGenerateRequest(BaseModel):
    dry_run: bool = False


class ProcurementLabelGenerateResponse(BaseModel):
    preview: ProcurementLabelOrderPreview
    generated: bool
    artifact_version: int | None = None
    zip_filename: str | None = None
    zip_url: str | None = None
    disk_file_id: str | None = None


class ProcurementLabelApproveResponse(BaseModel):
    item_id: str
    status: str
    artifact_version: int | None = None
    zip_url: str | None = None
