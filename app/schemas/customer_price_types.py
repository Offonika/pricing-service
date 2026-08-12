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


class CustomerPriceTypeAdvisoryPreviewRequest(BaseModel):
    return_character: str | None = Field(default=None, max_length=500)
    period_mismatch: str | None = Field(default=None, max_length=500)
    behavior_group: str | None = Field(default=None, max_length=100)
    notification_event: Literal["presignal", "price_type_changed", "recovery"] | None = None
    current_level: str | None = Field(default=None, max_length=100)


class CustomerPriceTypeOrderLampResponse(BaseModel):
    key: str
    severity: Literal["none", "info", "warning", "critical", "review"]
    title: str
    manager_action: str
    visible: bool
    blocks_fulfillment: bool


class CustomerPriceTypeNotificationDraftResponse(BaseModel):
    event: Literal["presignal", "price_type_changed", "recovery"]
    text: str
    channel_candidates: list[str] = Field(default_factory=list)
    approval_status: Literal["requires_approval"]
    send_allowed: bool


class CustomerPriceTypeAdvisoryPreviewResponse(BaseModel):
    mode: Literal["shadow"] = "shadow"
    onec_write_allowed: bool = False
    order_lamp: CustomerPriceTypeOrderLampResponse
    notification: CustomerPriceTypeNotificationDraftResponse | None = None


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
    sale_document_count_12m: int = 0
    sales_amount_12m: Decimal | None = None
    last_sale_at: date | None = None
    is_working: bool = False
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


CustomerPriceTypePortfolioBucket = Literal["working_bronze", "review_queue", "all"]


class CustomerPriceTypePortfolioItem(BaseModel):
    counterparty_ref: str
    counterparty_code: str
    counterparty_name: str | None = None
    department_name: str | None = None
    owner_name: str | None = None
    bucket: Literal["working_bronze", "review_queue"]
    expected_bucket: Literal["working_bronze", "review_queue"]
    expected_price_type: str | None = None
    current_price_type: str | None = None
    price_type_variant: str | None = None
    working_contracts: list[CustomerPriceTypeContractCandidate] = Field(default_factory=list)
    action_required: bool = False
    system_recommendation: str | None = None
    recommended_price_type: str | None = None
    source_status: str
    stop_factors: list[str] = Field(default_factory=list)
    review_status: Literal["ready", "business_conflict", "technical_incomplete", "missing_snapshot"]
    case_id: int | None = None
    case_type: str | None = None
    case_stage: str | None = None
    reconciliation_status: Literal["match", "mismatch", "missing_snapshot"]


class CustomerPriceTypePortfolioResponse(CustomerPriceTypeEnvelope):
    batch_key: str
    batch_label: str
    expected_counts: dict[str, int] = Field(default_factory=dict)
    counts: dict[str, int] = Field(default_factory=dict)
    review_status_counts: dict[str, int] = Field(default_factory=dict)
    mismatch_count: int = 0
    total: int
    limit: int
    offset: int
    payload: list[CustomerPriceTypePortfolioItem] = Field(default_factory=list)


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
    case_history: list[CustomerPriceTypeCaseItem] = Field(default_factory=list)
    history: list[CustomerPriceTypeSnapshotResponse] = Field(default_factory=list)


CustomerPriceTypeSearchState = Literal["no_change", "change_proposed", "data_issue"]


class CustomerPriceTypeProfileSearchItem(BaseModel):
    counterparty_ref: str
    counterparty_code: str | None = None
    counterparty_name: str | None = None
    current_price_type: str | None = None
    recommended_price_type: str | None = None
    result_state: CustomerPriceTypeSearchState
    result_label: str
    can_review: bool = False
    quality_sample_id: int | None = None
    quality_sample_status: Literal["pending", "reviewed"] | None = None


class CustomerPriceTypeProfileSearchResponse(CustomerPriceTypeEnvelope):
    total: int
    limit: int
    offset: int
    payload: list[CustomerPriceTypeProfileSearchItem] = Field(default_factory=list)


class CustomerPriceTypeDataIssueItem(BaseModel):
    counterparty_ref: str
    counterparty_code: str | None = None
    counterparty_name: str | None = None
    current_price_type: str | None = None
    issue_source: Literal["calculation", "expert"]
    issue_text: str
    reported_by: str | None = None
    reported_at: datetime | None = None
    comment: str | None = None
    case_id: int | None = None


class CustomerPriceTypeDataIssueListResponse(CustomerPriceTypeEnvelope):
    total: int
    limit: int
    offset: int
    payload: list[CustomerPriceTypeDataIssueItem] = Field(default_factory=list)


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
    review_result: Literal["correct", "incorrect", "data_issue"]
    correct_group: CustomerPriceTypeQualityGroup | None = None
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
    review_result: Literal["correct", "incorrect", "data_issue"] | None = None
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
