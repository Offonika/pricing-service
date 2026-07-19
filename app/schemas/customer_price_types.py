"""Pydantic contracts for the customer price-type read-only API."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field


class CustomerPriceTypeEnvelope(BaseModel):
    run_id: int | None
    snapshot_month: date | None
    ruleset_version: str | None
    source_status: str


class CustomerPriceTypeSummary(BaseModel):
    profile_count: int = 0
    actionable_count: int = 0
    levels: dict[str, int] = Field(default_factory=dict)
    recommendations: dict[str, int] = Field(default_factory=dict)
    source_statuses: dict[str, int] = Field(default_factory=dict)
    review_types: dict[str, int] = Field(default_factory=dict)
    departments: dict[str, int] = Field(default_factory=dict)


class CustomerPriceTypeSummaryResponse(CustomerPriceTypeEnvelope):
    summary: CustomerPriceTypeSummary


class CustomerPriceTypeWorklistsResponse(CustomerPriceTypeEnvelope):
    worklists: dict[str, int] = Field(default_factory=dict)


class CustomerPriceTypeSnapshotResponse(BaseModel):
    id: int
    run_id: int
    counterparty_ref: str
    snapshot_month: date
    ruleset_version: str
    current_price_type: str | None = None
    current_level: str | None = None
    price_type_variant: str | None = None
    contract_candidates: list[dict[str, Any]] = Field(default_factory=list)
    monthly_sales: dict[str, str] | None = None
    total_3m: Decimal | None = None
    last_month: Decimal | None = None
    economics: dict[str, Any] | None = None
    payments: dict[str, Any] | None = None
    returns: dict[str, Any] = Field(default_factory=dict)
    history: dict[str, Any] = Field(default_factory=dict)
    source_status: str
    source_statuses: dict[str, str] = Field(default_factory=dict)
    conflicts: list[str] = Field(default_factory=list)
    stop_factors: list[str] = Field(default_factory=list)
    system_recommendation: str
    recommended_price_type: str | None = None
    recommendation_reason: str
    action_required: bool
    case_type: str | None = None
    review_type: str | None = None
    reasons: list[str] = Field(default_factory=list)
    snapshot_hash: str
    money_visible: bool


class CustomerPriceTypeCaseItem(BaseModel):
    id: int
    case_key: str
    counterparty_ref: str
    counterparty_code: str | None = None
    counterparty_name: str | None = None
    snapshot_month: date
    stage: str
    case_type: str
    review_type: str | None = None
    reasons: list[str] = Field(default_factory=list)
    owner_ref: str | None = None
    owner_name: str | None = None
    department_ref: str | None = None
    department_name: str | None = None
    due_at: datetime | None = None
    system_recommendation: str
    recommended_price_type: str | None = None
    human_final_decision: str | None = None
    approval_status: str
    action_required: bool
    snapshot_hash: str
    version: int


class CustomerPriceTypeCaseListResponse(CustomerPriceTypeEnvelope):
    total: int
    limit: int
    offset: int
    payload: list[CustomerPriceTypeCaseItem] = Field(default_factory=list)


class CustomerPriceTypeCaseEventResponse(BaseModel):
    id: int
    event_type: str
    event_at: datetime
    actor: str
    source: str
    before_status: str | None = None
    after_status: str | None = None
    comment: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str


class CustomerPriceTypeCaseDetailResponse(CustomerPriceTypeEnvelope):
    case: CustomerPriceTypeCaseItem
    snapshot: CustomerPriceTypeSnapshotResponse
    events: list[CustomerPriceTypeCaseEventResponse] = Field(default_factory=list)


class CustomerPriceTypeProfileResponse(CustomerPriceTypeEnvelope):
    id: int
    counterparty_ref: str
    counterparty_code: str | None = None
    counterparty_name: str | None = None
    department_ref: str | None = None
    department_name: str | None = None
    owner_ref: str | None = None
    owner_name: str | None = None
    is_service_card: bool
    is_hygiene: bool
    master_data_flags: list[str] = Field(default_factory=list)
    latest_snapshot: CustomerPriceTypeSnapshotResponse | None = None
    open_case: CustomerPriceTypeCaseItem | None = None
    history: list[CustomerPriceTypeSnapshotResponse] = Field(default_factory=list)


class CustomerPriceTypeRunResponse(CustomerPriceTypeEnvelope):
    id: int
    run_key: str
    snapshot_month: date
    ruleset_version: str
    as_of: date
    window_start: date
    window_end: date
    source_statuses: dict[str, str] = Field(default_factory=dict)
    source_fingerprint: str
    input_count: int
    excluded_count: int
    calculated_count: int
    conflict_count: int
    actionable_count: int
    status: str
    error_summary: str | None = None
    started_at: datetime
    completed_at: datetime | None = None
