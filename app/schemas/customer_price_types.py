"""Pydantic contracts for the customer price-type read-only API."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

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


class CustomerPriceTypeContractCandidate(BaseModel):
    contract_ref: str | None = None
    contract_name: str | None = None
    price_type_name: str | None = None
    price_type_marked: bool = False
    price_type_missing: bool = False
    used_for_calculation: bool = False
    price_type_change_target: bool = False
    ignored_reason: str | None = None


class CustomerPriceTypeSnapshotResponse(BaseModel):
    id: int
    run_id: int
    counterparty_ref: str
    snapshot_month: date
    ruleset_version: str
    current_price_type: str | None = None
    current_level: str | None = None
    price_type_variant: str | None = None
    contract_candidates: list[CustomerPriceTypeContractCandidate] = Field(default_factory=list)
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


class CustomerPriceTypeCaseGuidance(BaseModel):
    title: str
    rules: str
    recommended_action: str
    expected_price_type: str
    manager_attention: list[str] = Field(default_factory=list)


class CustomerPriceTypeCaseDetailResponse(CustomerPriceTypeEnvelope):
    case: CustomerPriceTypeCaseItem
    snapshot: CustomerPriceTypeSnapshotResponse
    guidance: CustomerPriceTypeCaseGuidance | None = None
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


class CustomerPriceTypeSessionRequest(BaseModel):
    access_token: str = Field(min_length=1)
    domain: str = Field(min_length=1)
    member_id: str = Field(min_length=1)


class CustomerPriceTypeSessionUser(BaseModel):
    user_id: str
    name: str | None = None
    role: str
    can_view_money: bool


class CustomerPriceTypeSessionResponse(BaseModel):
    session_token: str
    token_type: str = "Bearer"
    expires_at: datetime
    expires_in: int
    user: CustomerPriceTypeSessionUser


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


CustomerPriceTypeQualityGroup = Literal[
    "manager_work",
    "isolate",
    "recovery",
    "data_check",
    "special_review",
    "downgrade_approval",
    "no_action",
]


class CustomerPriceTypeQualityPrepareRequest(BaseModel):
    snapshot_month: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}$")
    per_group: int = Field(default=30, ge=1, le=200)


class CustomerPriceTypeQualityPrepareResponse(CustomerPriceTypeEnvelope):
    created: int
    total: int
    per_group: int


class CustomerPriceTypeQualityReviewRequest(BaseModel):
    correct_group: CustomerPriceTypeQualityGroup
    comment: str | None = Field(default=None, max_length=2000)
    expected_version: int = Field(ge=1)


class CustomerPriceTypeQualitySampleResponse(BaseModel):
    id: int
    run_id: int
    snapshot_id: int
    counterparty_ref: str
    counterparty_code: str | None = None
    counterparty_name: str | None = None
    current_price_type: str | None = None
    recommended_price_type: str | None = None
    system_recommendation: str
    recommendation_reason: str
    stop_factors: list[str] = Field(default_factory=list)
    system_group: CustomerPriceTypeQualityGroup
    correct_group: CustomerPriceTypeQualityGroup | None = None
    status: Literal["pending", "reviewed"]
    selected_by: str
    selected_at: datetime
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    comment: str | None = None
    version: int


class CustomerPriceTypeQualitySampleListResponse(CustomerPriceTypeEnvelope):
    total: int
    limit: int
    offset: int
    payload: list[CustomerPriceTypeQualitySampleResponse] = Field(default_factory=list)


class CustomerPriceTypeQualityProfileResponse(BaseModel):
    id: int
    counterparty_ref: str
    counterparty_code: str | None = None
    counterparty_name: str | None = None
    department_ref: str | None = None
    department_name: str | None = None
    owner_ref: str | None = None
    owner_name: str | None = None
    master_data_flags: list[str] = Field(default_factory=list)


class CustomerPriceTypeQualitySampleDetailResponse(CustomerPriceTypeEnvelope):
    sample: CustomerPriceTypeQualitySampleResponse
    profile: CustomerPriceTypeQualityProfileResponse
    snapshot: CustomerPriceTypeSnapshotResponse


class CustomerPriceTypeQualityGroupMetrics(BaseModel):
    population_count: int
    selected_count: int
    reviewed_count: int
    true_positive: int
    false_positive: int
    false_negative: int
    precision: float | None = None
    recall: float | None = None


class CustomerPriceTypeQualityMetricsResponse(CustomerPriceTypeEnvelope):
    metrics_scope: Literal["portfolio", "special_review_only"]
    metrics_ready: bool
    population_count: int
    selected_count: int
    reviewed_count: int
    coverage: float
    override_rate: float
    critical_false_downgrade_count: int
    groups: dict[str, CustomerPriceTypeQualityGroupMetrics] = Field(default_factory=dict)
    matrix: dict[str, dict[str, int]] = Field(default_factory=dict)
