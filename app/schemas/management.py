from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel


class ManagementEnvelope(BaseModel):
    as_of: date | str
    freshness_status: str
    source_status: str


class ManagementComponentHealth(BaseModel):
    component: str
    freshness_status: str
    source_status: str
    latest_snapshot_date: date | None = None
    requested_date: date
    lag_days: int | None = None
    metrics: dict[str, Any]


class ManagementTaskPayload(BaseModel):
    rule_code: str
    source_type: str
    entity_ref: str
    entity_name: str | None = None
    severity: str
    owner_code: str
    created_by_code: str | None = None
    suppress_default_observers: bool = False
    allow_assignee_change_deadline: bool = False
    watcher_codes: list[str] = []
    title: str
    summary: str
    reaction_deadline_at: datetime
    due_at: datetime
    dedupe_key: str
    tags: list[str] = []
    metrics: dict[str, Any]
    references: list[dict[str, Any]] = []


class ReceivableCaseItem(BaseModel):
    snapshot_date: date
    segment: str
    owner_type: str
    recommendation: str
    counterparty_ref: str
    counterparty_name: str | None = None
    current_balance: Decimal
    aged_bucket: str
    activity_segment: str
    origin_document_ref: str | None = None
    origin_document_number: str | None = None
    origin_document_date: datetime | None = None
    origin_manager_ref: str | None = None
    origin_manager_name: str | None = None
    current_manager_ref: str | None = None
    current_manager_name: str | None = None
    department_ref: str | None = None
    department_name: str | None = None
    planned_payment_date: datetime | None = None
    credit_depth_days: int | None = None
    shipment_ban: bool | None = None
    payment_term_source: str | None = None
    due_date: datetime | None = None
    overdue_days: int | None = None
    is_overdue: bool = False
    chain_documents: list[dict[str, Any]] = []


class ReceivablesCaseListResponse(ManagementEnvelope):
    payload: list[ReceivableCaseItem]


class CounterpartyFolderRecommendationItem(BaseModel):
    snapshot_date: date
    counterparty_ref: str
    counterparty_code: str | None = None
    counterparty_name: str | None = None
    current_balance: Decimal
    current_folder_ref: str | None = None
    current_folder_name: str | None = None
    recommended_folder_ref: str | None = None
    recommended_folder_name: str | None = None
    debt_department_ref: str | None = None
    debt_department_name: str | None = None
    debt_document_ref: str | None = None
    debt_document_number: str | None = None
    debt_document_date: datetime | None = None
    debt_document_author_ref: str | None = None
    debt_document_author_name: str | None = None
    open_debt_documents: list[dict[str, Any]] = []
    origin_document_ref: str | None = None
    origin_document_number: str | None = None
    origin_document_date: datetime | None = None
    origin_manager_ref: str | None = None
    origin_manager_name: str | None = None
    current_manager_ref: str | None = None
    current_manager_name: str | None = None
    planned_payment_date: datetime | None = None
    credit_depth_days: int | None = None
    payment_term_source: str | None = None
    due_date: datetime | None = None
    overdue_days: int | None = None
    is_overdue: bool
    effective_credit_depth_days: int | None = None
    effective_payment_term_source: str | None = None
    effective_due_date: datetime | None = None
    effective_overdue_days: int | None = None
    status: str
    review_reason: str | None = None
    document_structure_status: str | None = None
    document_structure_open_amount: Decimal | None = None
    document_structure_sale_amount: Decimal | None = None
    document_structure_closing_amount: Decimal | None = None
    document_structure_order_ref: str | None = None
    document_structure_order_number: str | None = None
    document_structure_order_date: datetime | None = None
    document_structure_linked_documents: list[dict[str, Any]] = []


class CounterpartyFolderRecommendationResponse(ManagementEnvelope):
    report_revision: str
    summary: dict[str, Any]
    payload: list[CounterpartyFolderRecommendationItem]


class CounterpartyFolderSnapshotSyncResponse(ManagementEnvelope):
    summary: dict[str, Any]


class CounterpartyFolderChangeItem(BaseModel):
    snapshot_date: date
    previous_snapshot_date: date | None = None
    counterparty_ref: str
    counterparty_name: str | None = None
    old_folder_ref: str | None = None
    old_folder_name: str | None = None
    new_folder_ref: str | None = None
    new_folder_name: str | None = None
    current_balance: Decimal
    origin_document_ref: str | None = None
    origin_document_number: str | None = None
    origin_document_date: datetime | None = None
    recommended_folder_ref: str | None = None
    recommended_folder_name: str | None = None
    debt_department_ref: str | None = None
    debt_department_name: str | None = None


class CounterpartyFolderChangeResponse(ManagementEnvelope):
    previous_as_of: date | None = None
    report_revision: str
    summary: dict[str, Any]
    payload: list[CounterpartyFolderChangeItem]


class ReceivablesManagerSummaryItem(BaseModel):
    manager_ref: str | None = None
    manager_name: str | None = None
    counterparty_count: int
    total_balance: Decimal
    new_daily_count: int
    inactive_count: int
    employee_count: int
    fired_manager_count: int
    adjustment_candidates_count: int


class ReceivablesManagerSummaryResponse(ManagementEnvelope):
    payload: list[ReceivablesManagerSummaryItem]


class StaffingDailyItem(BaseModel):
    snapshot_date: date
    store_ref: str
    store_name: str | None = None
    shift_code: str
    planned_count: int
    assigned_count: int
    confirmed_count: int
    no_show_count: int
    deficit_count: int
    fill_rate: Decimal
    criticality: str
    deficit_role_counts: dict[str, Any] | None = None


class StaffingDailyResponse(ManagementEnvelope):
    payload: list[StaffingDailyItem]


class StaffingPeriodSummaryItem(BaseModel):
    store_ref: str
    store_name: str | None = None
    period_start: str
    period_end: str
    total_planned_count: int
    total_assigned_count: int
    total_confirmed_count: int
    total_no_show_count: int
    average_fill_rate: float
    days_with_deficit: int
    critical_days: int
    repeated_deficit_days: int
    forecast_deficit_days: dict[int, int]


class StaffingPeriodSummaryResponse(ManagementEnvelope):
    payload: list[StaffingPeriodSummaryItem]


class ManagementTaskPayloadListResponse(ManagementEnvelope):
    payload: list[ManagementTaskPayload]


class ManagementHealthResponse(ManagementEnvelope):
    status: str
    components: list[ManagementComponentHealth]


class TaskEfficiencyEmployeeItem(BaseModel):
    month_start: date
    month_end: date
    employee_bitrix_id: str | None = None
    employee_key: str | None = None
    employee_name: str | None = None
    metric_code: str = "personal_tasks_on_time_share"
    total_personal_tasks_with_deadline: int
    closed_on_time_personal_tasks: int
    late_closed_personal_tasks: int
    open_overdue_personal_tasks: int
    canceled_personal_tasks: int
    personal_tasks_on_time_share: Decimal | None = None
    bitrix_total_in_work_count: int | None = None
    bitrix_completed_tasks_count: int | None = None
    bitrix_task_remarks_count: int | None = None
    bitrix_effectiveness_pct: Decimal | None = None
    bitrix_effectiveness_source: str | None = None
    include_subtasks: bool = False
    min_task_count: int = 1
    is_metric_applicable: bool = True
    exclusion_reason: str | None = None
    source_scope: str | None = None
    calculation_note: str | None = None
    calculated_at: datetime | None = None


class TaskEfficiencyResponse(ManagementEnvelope):
    month: str
    month_start: date
    month_end: date
    note: str | None = None
    summary: dict[str, Any]
    payload: list[TaskEfficiencyEmployeeItem]


class RetailDirectorMonthlyKpiPayload(BaseModel):
    month: str
    title: str | None = None
    subtitle: str | None = None
    overall_signal: str | None = None
    close_status: str | None = None
    writeoff_amount: Decimal | None = None
    receipt_amount: Decimal | None = None
    shrinkage_amount: Decimal | None = None
    shrinkage_pct: Decimal | None = None
    matched_store_count: int | None = None
    kpi_index_sum: Decimal | None = None
    kpi_bonus_amount: Decimal | None = None
    to_pay: Decimal | None = None
    warnings: list[str] = []
    source_path: str | None = None


class RetailDirectorMonthlyKpiResponse(ManagementEnvelope):
    month: str
    payload: RetailDirectorMonthlyKpiPayload | None = None


class RetailCustomerPriceTypeRecommendation(BaseModel):
    counterparty_ref: str
    counterparty_code: str | None = None
    counterparty_name: str | None = None
    current_price_type: str | None = None
    current_level: str
    current_level_label: str
    recommended_price_type: str
    recommended_level: str
    recommended_level_label: str
    action: str
    action_label: str
    purchase_amount: Decimal
    net_sales_amount: Decimal
    previous_purchase_amount: Decimal
    previous_net_sales_amount: Decimal
    purchase_delta_amount: Decimal
    net_sales_delta_amount: Decimal
    purchase_delta_pct: Decimal | None = None
    net_sales_delta_pct: Decimal | None = None
    sales_amount: Decimal
    return_amount: Decimal
    document_count: int
    last_sale_at: datetime | None = None
    current_price_seen_at: datetime | None = None
    rule_note: str


class RetailCustomerPriceTypeRecommendationResponse(ManagementEnvelope):
    month: str
    previous_month: str
    month_start: date
    month_end: date
    summary: dict[str, Any]
    payload: list[RetailCustomerPriceTypeRecommendation]


class WeeklyKpiReportEmployee(BaseModel):
    employee_key: str
    employee_name: str
    role_code: str | None = None
    position_code: str | None = None
    position_name: str | None = None
    bitrix_user_id: str | None = None
    bitrix_box_user_id: str | None = None


class WeeklyKpiReportPeriod(BaseModel):
    week_start: date
    week_end: date
    source_as_of: date | None = None


class WeeklyKpiReportMetric(BaseModel):
    metric_code: str
    metric_name: str
    unit: str | None = None
    fact_value: Decimal
    plan_value: Decimal | None = None
    achievement_pct: Decimal | None = None
    bonus_preview_amount: Decimal | None = None
    previous_fact_value: Decimal | None = None
    delta_abs: Decimal | None = None
    delta_pct: Decimal | None = None
    signal: str | None = None
    source_system: str | None = None
    source_entity: str | None = None
    comment: str | None = None


class WeeklyKpiReportManifest(BaseModel):
    report_id: int
    report_key: str
    revision: int
    overall_signal: str | None = None
    summary_payload: dict[str, Any]
    employee: WeeklyKpiReportEmployee
    period: WeeklyKpiReportPeriod
    artifact_url: str


class WeeklyKpiReportDetail(WeeklyKpiReportManifest):
    metrics: list[WeeklyKpiReportMetric]


class WeeklyKpiReportListResponse(ManagementEnvelope):
    week_end: date
    payload: list[WeeklyKpiReportManifest]


class WeeklyKpiReportDetailResponse(ManagementEnvelope):
    week_end: date
    payload: WeeklyKpiReportDetail


class WeeklyKpiReportHealthResponse(ManagementEnvelope):
    week_end: date
    status: str
    report_count: int
    ready_count: int
    lifecycle_counts: dict[str, int]
    eligibility_counts: dict[str, int]
    artifact_counts: dict[str, int]
    latest_generated_at: datetime | None = None


class WeeklyManagerSalesReportPeriod(BaseModel):
    week_start: date
    week_end: date
    compare_week_start: date
    compare_week_end: date
    employee_snapshot_date: date
    employee_previous_date: date | None = None


class WeeklyManagerSalesReportArtifact(BaseModel):
    artifact_type: str
    title: str
    filename: str
    artifact_url: str
    sha256: str
    size_bytes: int
    message: str


class WeeklyManagerSalesReportManifest(BaseModel):
    report_key: str
    revision: str
    generated_at: datetime
    period: WeeklyManagerSalesReportPeriod
    manager_count: int
    attention_count: int
    employee_case_count: int
    cash_order_count: int
    artifacts: list[WeeklyManagerSalesReportArtifact]


class WeeklyManagerSalesReportResponse(ManagementEnvelope):
    week_end: date
    payload: WeeklyManagerSalesReportManifest


class WeeklyManagerSalesReportHealthResponse(ManagementEnvelope):
    week_end: date
    status: str
    report_key: str | None = None
    revision: str | None = None
    artifact_count: int
    manager_count: int
    attention_count: int
    employee_case_count: int
    cash_order_count: int
    generated_at: datetime | None = None
    error: str | None = None
