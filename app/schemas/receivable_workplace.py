from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field


class ReceivableWorkplaceStatusOption(BaseModel):
    value: str
    label: str
    scope: str = "common"


class ReceivableWorkplaceStaffOption(BaseModel):
    staff_ref: str
    staff_name: str
    department_ref: str | None = None
    department_name: str | None = None


class ReceivableWorkplaceDepartmentOption(BaseModel):
    department_ref: str
    department_name: str


class ReceivableWorkplaceCacheComponent(BaseModel):
    source_status: str
    cached_count: int = 0
    computed_at: datetime | None = None
    source_max_document_date: datetime | None = None
    source_lag_days: int | None = None


class ReceivableWorkplaceMetaResponse(BaseModel):
    latest_snapshot_date: date | None = None
    department_options: list[ReceivableWorkplaceDepartmentOption] = Field(default_factory=list)
    cache_status: dict[str, ReceivableWorkplaceCacheComponent] = Field(default_factory=dict)


class ReceivableWorkplaceDocument(BaseModel):
    document_ref: str | None = None
    document_number: str | None = None
    document_date: datetime | None = None
    amount: Decimal
    gross_amount: Decimal | None = None
    open_amount: Decimal | None = None
    closing_amount: Decimal | None = None
    return_amount: Decimal | None = None
    manager_name: str | None = None
    due_date: datetime | None = None
    overdue_days: int | None = None
    is_overdue: bool = False
    selection_rule: str | None = None
    statement_balance_after: Decimal | None = None
    match_details: list[dict[str, Any]] = Field(default_factory=list)
    document_structure_status: str | None = None


class ReceivableWorkplaceItem(BaseModel):
    snapshot_date: date
    stable_key: str
    counterparty_ref: str
    counterparty_code: str | None = None
    counterparty_name: str | None = None
    bitrix_detail_url: str | None = None
    department_ref: str | None = None
    department_name: str | None = None
    responsible_ref: str | None = None
    responsible_name: str | None = None
    phone: str | None = None
    phone_status: str
    current_balance: Decimal
    overdue_amount: Decimal
    effective_due_date: datetime | None = None
    effective_overdue_days: int | None = None
    oldest_overdue_date: datetime | None = None
    invoice_count: int
    overdue_invoice_count: int
    promised_payment_date: datetime | None = None
    last_contact_at: datetime | None = None
    contacted_staff_ref: str | None = None
    contacted_staff_name: str | None = None
    status: str
    next_action_date: datetime | None = None
    payment_postponed: bool = False
    payment_postponed_count: int = 0
    comment: str | None = None
    needs_call_today: bool
    no_phone_marker: bool
    needs_credit_depth_default: bool
    criticality: str
    documents: list[ReceivableWorkplaceDocument] = Field(default_factory=list)
    staff_options: list[ReceivableWorkplaceStaffOption] = Field(default_factory=list)


class ReceivableWorkplaceSummary(BaseModel):
    row_count: int
    total_receivable: Decimal
    total_overdue: Decimal
    overdue_over_30_amount: Decimal
    overdue_over_90_amount: Decimal
    need_call_today_amount: Decimal
    no_phone_count: int
    credit_depth_default_count: int


class ReceivableWorkplaceResponse(BaseModel):
    as_of: date
    freshness_status: str
    source_status: str
    summary: ReceivableWorkplaceSummary
    total_count: int
    visible_count: int
    summary_scope: str = "filtered_total"
    department_options: list[ReceivableWorkplaceDepartmentOption] = Field(default_factory=list)
    cache_status: dict[str, ReceivableWorkplaceCacheComponent] = Field(default_factory=dict)
    status_options: list[ReceivableWorkplaceStatusOption]
    payload: list[ReceivableWorkplaceItem]


class ReceivableWorkplaceActionRequest(BaseModel):
    action_id: str | None = None
    status: str | None = None
    contacted_staff_ref: str | None = None
    contacted_staff_name: str | None = None
    promised_payment_date: date | None = None
    last_contact_at: date | None = None
    next_action_date: date | None = None
    payment_postponed: bool | None = None
    comment: str | None = None


class ReceivableWorkplaceActionResponse(BaseModel):
    item: ReceivableWorkplaceItem
    event: dict[str, Any]
    cache_status: dict[str, ReceivableWorkplaceCacheComponent] = Field(default_factory=dict)
