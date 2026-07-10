from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field

ExecutiveAccessLevel = Literal["full", "domain"]


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


class ExecutiveProfitLossExpenseBreakdownRow(BaseModel):
    key: str
    label: str
    amount: Decimal = Decimal("0")
    movement_count: int = 0
    review_count: int = 0
    source_status: str = "ready"
    recognition_method: str = "cashflow_fallback"
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
    by_store: list[ExecutiveProfitLossBreakdownRow] = Field(default_factory=list)
    by_manager: list[ExecutiveProfitLossBreakdownRow] = Field(default_factory=list)
    expense_source_status: str = "source_missing"
    expense_breakdown: list[ExecutiveProfitLossExpenseBreakdownRow] = Field(default_factory=list)
    expense_open_questions: list[ExecutiveProfitLossOpenQuestion] = Field(default_factory=list)
    filters: dict[str, Any] = Field(default_factory=dict)


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
