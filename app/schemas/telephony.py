from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, Field


class TelephonyEnvelope(BaseModel):
    as_of: date | None = None
    freshness_status: str
    source_status: str


class TelephonyUserLineItem(BaseModel):
    snapshot_date: date
    mapping_source: str
    user_ref_hex: str
    user_name: str | None = None
    physical_person_ref_hex: str | None = None
    physical_person_name: str | None = None
    computer_name: str | None = None
    extension: str | None = None
    store_ref_hex: str | None = None
    store_code: str | None = None
    store_name: str | None = None
    department_ref_hex: str | None = None
    department_code: str | None = None
    department_name: str | None = None
    employment_status: str | None = None
    staff_store_ref: str | None = None
    staff_store_name: str | None = None
    staff_department_ref: str | None = None
    staff_department_name: str | None = None
    bitrix_user_id: str | None = None
    bitrix_full_name: str | None = None
    mdm_employee_code: str | None = None
    bitrix_status: str | None = None
    is_marked: bool
    has_extension: bool
    has_bitrix: bool


class TelephonyUserLineMapResponse(TelephonyEnvelope):
    snapshot_date: date | None = None
    payload: list[TelephonyUserLineItem]


class TelephonyRetailLineItem(BaseModel):
    line_id: str
    phone_number: str | None = None
    store_id: str
    store_name: str
    mapping_mode: str
    active_user_count: int
    total_user_count: int
    store_names: list[str] = Field(default_factory=list)
    employee_names: list[str] = Field(default_factory=list)
    bitrix_user_ids: list[str] = Field(default_factory=list)
    primary_bitrix_user_id: str | None = None
    primary_employee_name: str | None = None
    primary_store_name: str | None = None


class TelephonyRetailLineMapResponse(TelephonyEnvelope):
    snapshot_date: date | None = None
    payload: list[TelephonyRetailLineItem]


class TelephonyHealthResponse(TelephonyEnvelope):
    status: str
    snapshot_date: date | None = None
    lag_days: int | None = None
    metrics: dict[str, Any]
