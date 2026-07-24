from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field

ExecutiveAccessLevel = Literal["full", "domain"]
ExecutiveManagementBalanceView = Literal["closed", "operational"]


class ExecutiveDashboardMetric(BaseModel):
    key: str
    label: str
    value: Decimal | int | float | str | None = None
    unit: str | None = None
    tone: str = "neutral"
    masked: bool = False
    source_status: str = "ready"


class ExecutiveDashboardBlock(BaseModel):
    key: str
    title: str
    source_status: str
    freshness_status: str
    as_of: date | datetime | None = None
    summary: dict[str, Any] = Field(default_factory=dict)
    metrics: list[ExecutiveDashboardMetric] = Field(default_factory=list)
    drilldown_url: str | None = None


class ExecutiveSourceStatus(BaseModel):
    source_key: str
    title: str
    source_status: str
    freshness_status: str
    as_of: date | datetime | None = None
    max_lag_days: int | None = None
    note: str | None = None


class ExecutiveDashboardAction(BaseModel):
    stable_key: str
    business_date: date
    domain: str
    severity: str
    title: str
    description: str | None = None
    amount: Decimal | None = None
    currency: str = "RUB"
    responsible_bitrix_user_id: str | None = None
    deadline_at: datetime | None = None
    status: str = "open"
    source_system: str
    source_ref: str | None = None
    dedupe_key: str
    drilldown_url: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class ExecutiveDashboardResponse(BaseModel):
    as_of: date
    generated_at: datetime
    freshness_status: str
    source_status: str
    access_level: ExecutiveAccessLevel
    roles: list[str] = Field(default_factory=list)
    allowed_blocks: list[str] = Field(default_factory=list)
    allowed_action_domains: list[str] = Field(default_factory=list)
    blocks: list[ExecutiveDashboardBlock]
    source_freshness: list[ExecutiveSourceStatus]
    top_actions: list[ExecutiveDashboardAction]
    summary: dict[str, Any] = Field(default_factory=dict)


class ExecutiveDashboardActionsResponse(BaseModel):
    as_of: date
    freshness_status: str
    source_status: str
    total_count: int
    payload: list[ExecutiveDashboardAction]


class ExecutiveManagementBalanceLineItem(BaseModel):
    key: str
    label: str
    section: Literal["asset", "liability", "equity"]
    amount: Decimal | None = None
    delta_previous: Decimal | None = None
    source_key: str
    source_status: str
    source_as_of: date | None = None
    note: str | None = None
    source_amount: Decimal | None = None
    adjustment_amount: Decimal | None = None
    adjusted_amount: Decimal | None = None
    recognition_method: str | None = None
    estimated_count: int = 0


class ExecutiveManagementBalanceResponse(BaseModel):
    month: str
    balance_date: date
    view: ExecutiveManagementBalanceView
    version: int
    status: str
    source_status: str
    freshness_status: str
    generated_at: datetime
    closed_at: datetime | None = None
    closed_by: str | None = None
    currency: str = "RUB"
    assets: list[ExecutiveManagementBalanceLineItem] = Field(default_factory=list)
    liabilities: list[ExecutiveManagementBalanceLineItem] = Field(default_factory=list)
    equity: list[ExecutiveManagementBalanceLineItem] = Field(default_factory=list)
    assets_total: Decimal = Decimal("0")
    liabilities_total: Decimal = Decimal("0")
    equity_total: Decimal = Decimal("0")
    liabilities_and_equity_total: Decimal = Decimal("0")
    imbalance_amount: Decimal = Decimal("0")
    can_close: bool = False
    validation_errors: list[dict[str, Any]] = Field(default_factory=list)
    source_summary: dict[str, Any] = Field(default_factory=dict)
    available_months: list[str] = Field(default_factory=list)
    note: str | None = None


class ExecutiveManagementBalanceTurnoverLine(BaseModel):
    key: str
    label: str
    section: Literal["asset", "liability", "equity"]
    opening_balance: Decimal | None = None
    debit_turnover: Decimal | None = None
    credit_turnover: Decimal | None = None
    closing_balance: Decimal | None = None
    reconciliation_difference: Decimal | None = None
    turnover_method: Literal["net_change_from_snapshots"] = "net_change_from_snapshots"
    source_key: str
    source_status: str
    source_as_of: date | None = None
    note: str | None = None


class ExecutiveManagementBalanceTurnoverTotal(BaseModel):
    section: Literal["asset", "liability", "equity"]
    label: str
    opening_balance: Decimal = Decimal("0")
    debit_turnover: Decimal = Decimal("0")
    credit_turnover: Decimal = Decimal("0")
    closing_balance: Decimal = Decimal("0")
    reconciliation_difference: Decimal = Decimal("0")
    unknown_line_count: int = 0


class ExecutiveManagementBalanceTurnoverResponse(BaseModel):
    month: str
    date_from: date
    date_to: date
    view: ExecutiveManagementBalanceView
    opening_version: int
    closing_version: int
    opening_status: str
    closing_status: str
    opening_validation_error_count: int = 0
    opening_content_sha256: str
    closing_content_sha256: str
    turnover_method: Literal["net_change_from_snapshots"] = "net_change_from_snapshots"
    source_scope: Literal["onec_ut_10_3_plus_bp_accrued_taxes"] = (
        "onec_ut_10_3_plus_bp_accrued_taxes"
    )
    source_status: str
    currency: str = "RUB"
    lines: list[ExecutiveManagementBalanceTurnoverLine] = Field(default_factory=list)
    totals: list[ExecutiveManagementBalanceTurnoverTotal] = Field(default_factory=list)
    excluded_lines: list[dict[str, Any]] = Field(default_factory=list)
    opening_imbalance_amount: Decimal = Decimal("0")
    closing_imbalance_amount: Decimal = Decimal("0")
    opening_scope_imbalance_amount: Decimal = Decimal("0")
    closing_scope_imbalance_amount: Decimal = Decimal("0")
    unknown_line_count: int = 0
    note: str


class ExecutiveManagementBalanceCloseRequest(BaseModel):
    confirm: bool
    note: str | None = Field(default=None, max_length=1000)


class ExecutiveServiceAccrualItem(BaseModel):
    id: int
    month: str
    recognition_date: date
    counterparty_ref: str
    counterparty_name: str
    contract_ref: str
    contract_name: str
    expense_line_key: str
    expense_line_label: str
    status: str
    recognition_method: str
    recognized_amount_rub: Decimal
    payment_amount_rub: Decimal
    cashflow_expense_replaced_rub: Decimal
    source_status: str
    source_as_of: date | None = None
    note: str | None = None


class ExecutiveServiceAccrualListResponse(BaseModel):
    month: str
    source_status: str
    freshness_status: str
    total_count: int
    recognized_amount_rub: Decimal = Decimal("0")
    payment_amount_rub: Decimal = Decimal("0")
    estimated_count: int = 0
    items: list[ExecutiveServiceAccrualItem] = Field(default_factory=list)


class ExecutiveCashflowPeriodRatio(BaseModel):
    key: str
    label: str
    value: Decimal | None = None
    unit: str | None = None
    tone: str = "neutral"
    note: str | None = None


class ExecutiveCashflowPeriodBreakdownRow(BaseModel):
    key: str
    label: str
    inflow_amount: Decimal = Decimal("0")
    outflow_amount: Decimal = Decimal("0")
    net_amount: Decimal = Decimal("0")
    movement_count: int = 0
    review_count: int = 0
    meta: dict[str, Any] = Field(default_factory=dict)


class ExecutiveCashflowDailyRow(BaseModel):
    business_date: date
    inflow_amount: Decimal = Decimal("0")
    outflow_amount: Decimal = Decimal("0")
    net_amount: Decimal = Decimal("0")
    external_net_amount: Decimal = Decimal("0")
    internal_net_amount: Decimal = Decimal("0")
    movement_count: int = 0
    review_count: int = 0


class ExecutiveCashflowQualityIssue(BaseModel):
    issue_key: str
    issue_type: str
    issue_label: str
    severity: str
    business_date: date
    amount_abs: Decimal = Decimal("0")
    description: str | None = None
    proposed_action: str | None = None
    status: str = "open"
    document_number: str | None = None
    bitrix_task_id: str | None = None
    task_status: str | None = None
    drilldown_url: str | None = None


class ExecutiveCashflowPeriodResponse(BaseModel):
    date_from: date
    date_to: date
    generated_at: datetime | None = None
    source_status: str
    freshness_status: str
    note: str | None = None
    totals: dict[str, Decimal | int | None] = Field(default_factory=dict)
    ratios: list[ExecutiveCashflowPeriodRatio] = Field(default_factory=list)
    cash_position: dict[str, Any] = Field(default_factory=dict)
    daily: list[ExecutiveCashflowDailyRow] = Field(default_factory=list)
    by_group: list[ExecutiveCashflowPeriodBreakdownRow] = Field(default_factory=list)
    by_article: list[ExecutiveCashflowPeriodBreakdownRow] = Field(default_factory=list)
    by_cash_account: list[ExecutiveCashflowPeriodBreakdownRow] = Field(default_factory=list)
    by_currency: list[ExecutiveCashflowPeriodBreakdownRow] = Field(default_factory=list)
    quality_issues: list[ExecutiveCashflowQualityIssue] = Field(default_factory=list)
    filters: dict[str, Any] = Field(default_factory=dict)


class ExecutiveProfitLossLineItem(BaseModel):
    key: str
    label: str
    amount: Decimal | None = None
    unit: str | None = "RUB"
    line_type: str = "metric"
    tone: str = "neutral"
    source_status: str = "ready"
    note: str | None = None


class ExecutiveProfitLossRatio(BaseModel):
    key: str
    label: str
    value: Decimal | None = None
    unit: str | None = None
    tone: str = "neutral"
    note: str | None = None


class ExecutiveProfitLossBreakdownRow(BaseModel):
    key: str
    label: str
    revenue: Decimal = Decimal("0")
    cost_of_sales: Decimal = Decimal("0")
    gross_profit: Decimal = Decimal("0")
    sales_count: Decimal = Decimal("0")
    row_count: int = 0
    gross_margin_pct: Decimal | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class ExecutiveProfitLossDailyRow(ExecutiveProfitLossBreakdownRow):
    business_date: date


class ExecutiveProfitLossMonthlyRow(BaseModel):
    month: str
    revenue: Decimal = Decimal("0")
    gross_profit: Decimal | None = None
    operating_expenses: Decimal | None = None
    operating_profit: Decimal | None = None
    net_profit: Decimal | None = None
    gross_margin_pct: Decimal | None = None
    operating_margin_pct: Decimal | None = None
    net_profit_margin_pct: Decimal | None = None
    comparison_net_profit: Decimal | None = None
    source_status: str = "source_missing"
    is_preliminary: bool = True
    note: str | None = None


class ExecutiveProfitLossExpenseBreakdownRow(BaseModel):
    key: str
    label: str
    amount: Decimal = Decimal("0")
    movement_count: int = 0
    review_count: int = 0
    source_status: str = "ready"
    recognition_method: str = "cashflow_fallback"
    cashflow_amount: Decimal | None = None
    recognized_amount: Decimal | None = None
    adjustment_amount: Decimal | None = None
    estimated_count: int = 0
    meta: dict[str, Any] = Field(default_factory=dict)


class ExecutiveProfitLossOpenQuestion(BaseModel):
    key: str
    label: str
    amount: Decimal = Decimal("0")
    reason: str
    proposed_action: str | None = None
    movement_count: int = 0
    review_count: int = 0
    source_status: str = "partial"
    recognition_method: str = "cashflow_fallback"
    meta: dict[str, Any] = Field(default_factory=dict)


class ExecutiveProfitLossInventoryHistoryItem(BaseModel):
    month: str
    source_status: str = "ready"
    writeoff_amount: Decimal | None = None
    receipt_amount: Decimal | None = None
    loss_amount: Decimal | None = None
    loss_pct: Decimal | None = None


class ExecutiveProfitLossInventoryStore(BaseModel):
    store_ref: str
    store_name: str
    sales_amount: Decimal | None = None
    writeoff_amount: Decimal | None = None
    receipt_amount: Decimal | None = None
    loss_amount: Decimal | None = None
    loss_pct: Decimal | None = None
    norm_pct: Decimal | None = None
    variance_to_norm_pct: Decimal | None = None
    above_norm: bool = False
    source_status: str = "ready"
    has_operations: bool = False


class ExecutiveProfitLossInventoryDocument(BaseModel):
    stable_key: str
    operation_kind: str
    operation_label: str
    document_type: str
    document_ref: str
    document_number: str
    document_date: date | None = None
    store_ref: str
    store_name: str
    amount: Decimal
    effect_amount: Decimal


class ExecutiveProfitLossInventoryAction(BaseModel):
    stable_key: str
    action_type: str
    severity: str
    title: str
    description: str
    amount: Decimal | None = None
    store_ref: str | None = None
    store_name: str | None = None
    responsible_name: str | None = None
    recommended_action: str


class ExecutiveProfitLossInventoryDataQuality(BaseModel):
    source_status: str = "source_missing"
    approved_store_count: int = 0
    source_store_count: int = 0
    matched_store_count: int = 0
    unmatched_store_count: int = 0
    source_document_count: int = 0
    matched_document_count: int = 0
    unmatched_document_count: int = 0
    unmatched_writeoff_amount: Decimal = Decimal("0")
    unmatched_receipt_amount: Decimal = Decimal("0")
    excluded_store_count: int = 0
    excluded_document_count: int = 0
    excluded_writeoff_amount: Decimal = Decimal("0")
    excluded_receipt_amount: Decimal = Decimal("0")
    store_scope_status: str = "unknown"
    store_scope_source: str | None = None
    store_scope_month: str | None = None
    norm_source_status: str = "unknown"
    norm_source: str | None = None


class ExecutiveProfitLossInventoryOwner(BaseModel):
    employee_key: str | None = None
    employee_bitrix_id: str | None = None
    employee_name: str | None = None
    role_code: str | None = None


class ExecutiveProfitLossInventoryLoss(BaseModel):
    schema_version: int = 1
    month: str
    source_status: str = "source_missing"
    detail_source_status: str = "source_missing"
    writeoff_amount: Decimal | None = None
    receipt_amount: Decimal | None = None
    loss_amount: Decimal | None = None
    loss_pct: Decimal | None = None
    norm_pct: Decimal | None = None
    variance_to_norm_pct: Decimal | None = None
    matched_store_count: int | None = None
    previous_month: ExecutiveProfitLossInventoryHistoryItem | None = None
    average_loss_amount_3m: Decimal | None = None
    average_loss_pct_3m: Decimal | None = None
    history_source_status: str = "source_missing"
    history: list[ExecutiveProfitLossInventoryHistoryItem] = Field(default_factory=list)
    stores: list[ExecutiveProfitLossInventoryStore] = Field(default_factory=list)
    top_documents: list[ExecutiveProfitLossInventoryDocument] = Field(default_factory=list)
    actions: list[ExecutiveProfitLossInventoryAction] = Field(default_factory=list)
    data_quality: ExecutiveProfitLossInventoryDataQuality = Field(
        default_factory=ExecutiveProfitLossInventoryDataQuality
    )
    owner: ExecutiveProfitLossInventoryOwner | None = None
    warnings: list[str] = Field(default_factory=list)
    note: str | None = None


class ExecutiveProfitLossPeriodResponse(BaseModel):
    date_from: date
    date_to: date
    generated_at: datetime | None = None
    source_status: str
    freshness_status: str
    note: str | None = None
    totals: dict[str, Decimal | int | None] = Field(default_factory=dict)
    ratios: list[ExecutiveProfitLossRatio] = Field(default_factory=list)
    lines: list[ExecutiveProfitLossLineItem] = Field(default_factory=list)
    daily: list[ExecutiveProfitLossDailyRow] = Field(default_factory=list)
    monthly: list[ExecutiveProfitLossMonthlyRow] = Field(default_factory=list)
    by_store: list[ExecutiveProfitLossBreakdownRow] = Field(default_factory=list)
    by_manager: list[ExecutiveProfitLossBreakdownRow] = Field(default_factory=list)
    expense_source_status: str = "source_missing"
    expense_breakdown: list[ExecutiveProfitLossExpenseBreakdownRow] = Field(default_factory=list)
    expense_open_questions: list[ExecutiveProfitLossOpenQuestion] = Field(default_factory=list)
    inventory_loss: ExecutiveProfitLossInventoryLoss | None = None
    filters: dict[str, Any] = Field(default_factory=dict)


class ExecutiveSalesDailyRow(BaseModel):
    business_date: date
    actual_revenue: Decimal | None = None
    forecast_revenue: Decimal | None = None


class ExecutiveSalesMonthlyRow(BaseModel):
    month: str
    revenue: Decimal = Decimal("0")
    gross_profit: Decimal = Decimal("0")
    sales_count: Decimal = Decimal("0")
    gross_margin_pct: Decimal | None = None
    forecast_revenue: Decimal | None = None
    comparison_sales_count: Decimal | None = None


class ExecutiveSalesBreakdownRow(BaseModel):
    key: str
    label: str
    revenue: Decimal = Decimal("0")
    gross_profit: Decimal = Decimal("0")
    sales_count: Decimal = Decimal("0")
    gross_margin_pct: Decimal | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class ExecutiveSalesFilterOption(BaseModel):
    key: str
    label: str


class ExecutiveSalesPlanContext(BaseModel):
    source_status: str
    period_month: str
    revision_no: int | None = None
    snapshot_id: str | None = None
    frozen_at: datetime | None = None
    scope_type: str
    scope_key: str | None = None
    approved_revenue: Decimal | None = None
    approved_margin_pct: Decimal | None = None
    approved_gross_profit: Decimal | None = None
    comparison_basis: str = "not_applicable"
    comparison_revenue: Decimal | None = None
    plan_attainment_pct: Decimal | None = None
    note: str | None = None


class ExecutiveSalesDiagnosticKpi(BaseModel):
    key: str
    value: Decimal | int | None = None
    unit: str
    source_status: str
    note: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class ExecutiveSalesPeriodResponse(BaseModel):
    month: str
    date_from: date
    date_to: date
    as_of: date | None = None
    generated_at: datetime | None = None
    source_status: str
    freshness_status: str
    forecast_status: str = "not_applicable"
    plan_status: str = "source_missing"
    note: str | None = None
    forecast_note: str | None = None
    plan_note: str | None = None
    plan: ExecutiveSalesPlanContext | None = None
    diagnostic_kpis: list[ExecutiveSalesDiagnosticKpi] = Field(default_factory=list)
    totals: dict[str, Decimal | int | None] = Field(default_factory=dict)
    comparison: dict[str, Decimal | int | None] = Field(default_factory=dict)
    daily: list[ExecutiveSalesDailyRow] = Field(default_factory=list)
    monthly: list[ExecutiveSalesMonthlyRow] = Field(default_factory=list)
    by_store: list[ExecutiveSalesBreakdownRow] = Field(default_factory=list)
    by_manager: list[ExecutiveSalesBreakdownRow] = Field(default_factory=list)
    stores: list[ExecutiveSalesFilterOption] = Field(default_factory=list)
    managers: list[ExecutiveSalesFilterOption] = Field(default_factory=list)
    filters: dict[str, Any] = Field(default_factory=dict)


class ExecutiveOnlineStoreDailyRow(BaseModel):
    business_date: date
    visits: int = 0
    visitors: int = 0
    purchases: int = 0
    click_buy: int = 0
    begin_checkout: int = 0
    phone_clicks: int = 0
    site_searches: int = 0
    purchase_conversion_pct: Decimal = Decimal("0")


class ExecutiveOnlineStoreTrafficSourceRow(BaseModel):
    key: str
    label: str
    visits: int = 0
    visitors: int = 0
    purchases: int = 0
    purchase_conversion_pct: Decimal = Decimal("0")


class ExecutiveOnlineStoreLandingPageRow(BaseModel):
    url: str
    visits: int = 0
    visitors: int = 0
    purchases: int = 0
    click_buy: int = 0
    begin_checkout: int = 0
    purchase_conversion_pct: Decimal = Decimal("0")


class ExecutiveOnlineStorePeriodResponse(BaseModel):
    date_from: date
    date_to: date
    compare_date_from: date
    compare_date_to: date
    generated_at: datetime
    source_status: str = "ready"
    freshness_status: str = "fresh"
    counter_id: str
    site: str = "master-mobile.ru"
    note: str | None = None
    totals: dict[str, Decimal | int | str | None] = Field(default_factory=dict)
    comparison: dict[str, Decimal | int | str | None] = Field(default_factory=dict)
    daily: list[ExecutiveOnlineStoreDailyRow] = Field(default_factory=list)
    traffic_sources: list[ExecutiveOnlineStoreTrafficSourceRow] = Field(default_factory=list)
    landing_pages: list[ExecutiveOnlineStoreLandingPageRow] = Field(default_factory=list)


class BitrixExecutiveDashboardSessionRequest(BaseModel):
    access_token: str = Field(min_length=1)
    domain: str = Field(min_length=1)
    member_id: str = Field(min_length=1)


class BitrixExecutiveDashboardUser(BaseModel):
    user_id: str
    name: str | None = None


class BitrixExecutiveDashboardSessionResponse(BaseModel):
    session_token: str
    token_type: str = "bearer"
    expires_at: datetime
    expires_in: int
    user: BitrixExecutiveDashboardUser
    access_level: ExecutiveAccessLevel
    roles: list[str] = Field(default_factory=list)
    allowed_blocks: list[str] = Field(default_factory=list)
    allowed_action_domains: list[str] = Field(default_factory=list)
