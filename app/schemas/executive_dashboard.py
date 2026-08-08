from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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


ExecutiveInstrumentLifecycle = Literal[
    "planned",
    "procurement",
    "inventory_pending",
    "active",
    "draining",
    "decommissioned",
    "unknown",
]
ExecutiveInstrumentHealth = Literal[
    "ready",
    "warning",
    "critical",
    "not_monitored",
    "maintenance",
    "decommissioned",
]
ExecutiveInstrumentConnectivity = Literal[
    "online",
    "offline",
    "channel_unavailable",
    "not_monitored",
    "maintenance",
    "not_applicable",
]
ExecutiveInstrumentComponentStatus = Literal[
    "ready",
    "warning",
    "critical",
    "not_monitored",
    "not_configured",
    "running",
    "stopped",
    "degraded",
    "unknown",
]
ExecutiveInstrumentSourceStatus = Literal[
    "ready", "partial", "stale", "source_missing", "source_error"
]
ExecutiveInstrumentFreshnessStatus = Literal["fresh", "stale", "missing", "error"]


class ExecutiveInstrumentStrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExecutiveInstrumentMetrics(ExecutiveInstrumentStrictModel):
    cpu_used_pct: float | None = Field(default=None, ge=0, le=100)
    memory_used_pct: float | None = Field(default=None, ge=0, le=100)
    disk_free_pct: float | None = Field(default=None, ge=0, le=100)
    disk_free_gib: float | None = Field(default=None, ge=0)
    latency_ms: int | None = Field(default=None, ge=0)
    vcpu: int | None = Field(default=None, ge=0)
    uptime_seconds: int | None = Field(default=None, ge=0)


class ExecutiveInstrumentService(ExecutiveInstrumentStrictModel):
    service_key: str
    name: str
    component_kind: Literal[
        "windows",
        "sql_server",
        "sql_agent",
        "onec_cluster",
        "onec_database",
        "onec_publication",
        "integration",
        "disk",
        "service",
    ] = "service"
    status: ExecutiveInstrumentComponentStatus
    criticality: Literal["critical", "standard"] = "standard"
    last_verified_at: date | datetime | None = None
    last_success_at: date | datetime | None = None
    source_project: str | None = None


class ExecutiveInstrumentBackup(ExecutiveInstrumentStrictModel):
    status: Literal["ready", "warning", "critical", "not_configured"] = "not_configured"
    protected_datastores: int = Field(default=0, ge=0)
    unprotected_datastores: int = Field(default=0, ge=0)
    rpo_minutes: int | None = Field(default=None, ge=1)
    lag_minutes: int | None = Field(default=None, ge=0)
    last_backup_at: date | datetime | None = None
    last_full_backup_at: date | datetime | None = None
    last_differential_backup_at: date | datetime | None = None
    last_log_backup_at: date | datetime | None = None
    last_restore_test_at: date | datetime | None = None
    off_host_verified: bool = False
    readback_verified: bool = False


class ExecutiveInstrumentIntegration(ExecutiveInstrumentStrictModel):
    status: ExecutiveInstrumentComponentStatus = "not_configured"
    count: int = Field(default=0, ge=0)
    last_success_at: date | datetime | None = None


class ExecutiveInstrumentAccess(ExecutiveInstrumentStrictModel):
    status: Literal["ready", "warning", "not_configured"] = "not_configured"
    active_grants: int = Field(default=0, ge=0)
    pending_grants: int = Field(default=0, ge=0)
    review_required_grants: int = Field(default=0, ge=0)
    overdue_review_grants: int = Field(default=0, ge=0)
    mfa_review_count: int = Field(default=0, ge=0)
    unowned_credentials: int = Field(default=0, ge=0)
    attention_grant_count: int = Field(default=0, ge=0)
    next_review_at: date | None = None


ExecutiveInstrumentExchangeStatus = Literal[
    "ready",
    "warning",
    "critical",
    "not_configured",
]
ExecutiveInstrumentExchangeQueueStatus = Literal[
    "ready",
    "warning",
    "critical",
    "not_configured",
]
ExecutiveInstrumentExchangeStage = Literal[
    "checkauth",
    "init",
    "file",
    "import",
    "none",
]
ExecutiveInstrumentExchangeSourceStatus = Literal["ready", "partial", "not_configured"]


class ExecutiveInstrumentExchange(ExecutiveInstrumentStrictModel):
    status: ExecutiveInstrumentExchangeStatus = "not_configured"
    queue_items: int | None = Field(default=None, ge=0, strict=True)
    queue_status: ExecutiveInstrumentExchangeQueueStatus | None = None
    last_success_at: datetime | None = None
    last_error_at: datetime | None = None
    consecutive_failures: int | None = Field(default=None, ge=0, strict=True)
    active_job_seconds: int | None = Field(default=None, ge=0, strict=True)
    stage_last: ExecutiveInstrumentExchangeStage | None = None
    stage_file_missing_cycles: int | None = Field(default=None, ge=0, strict=True)
    platform_cpu_pct: float | None = Field(
        default=None,
        ge=0,
        le=100,
        allow_inf_nan=False,
        strict=True,
    )
    source_status: ExecutiveInstrumentExchangeSourceStatus = "not_configured"

    @field_validator("last_success_at", "last_error_at", mode="before")
    @classmethod
    def require_rfc3339_utc(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() != timedelta(0):
                raise ValueError("exchange timestamp must be timezone-aware UTC")
            return value
        if not isinstance(value, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)",
            value,
        ):
            raise ValueError("exchange timestamp must be RFC3339 UTC")
        return value

    @model_validator(mode="after")
    def preserve_not_configured_status(self) -> ExecutiveInstrumentExchange:
        if self.source_status == "not_configured" and self.status != "not_configured":
            raise ValueError("not-configured exchange source cannot raise exchange severity")
        return self


class ExecutiveInstrumentProblem(ExecutiveInstrumentStrictModel):
    problem_key: str
    category: Literal[
        "connectivity",
        "resources",
        "service",
        "backup",
        "access",
        "monitoring",
        "configuration",
    ]
    severity: Literal["critical", "warning", "info"]
    title: str
    evidence: list[str] = Field(default_factory=list)
    started_at: datetime | None = None
    recommended_action: str


class ExecutiveInstrumentDevice(ExecutiveInstrumentStrictModel):
    device_key: str
    name: str
    kind: str
    lifecycle_status: ExecutiveInstrumentLifecycle
    health_status: ExecutiveInstrumentHealth
    connectivity_status: ExecutiveInstrumentConnectivity
    criticality: Literal["critical", "standard"] = "standard"
    location: str
    purpose: list[str] = Field(default_factory=list)
    technical_owner_ids: list[str] = Field(default_factory=list)
    technical_owners: list[str] = Field(default_factory=list)
    business_owner: str | None = None
    last_attempted_at: datetime | None = None
    last_success_at: datetime | None = None
    incident_started_at: datetime | None = None
    outage_duration_seconds: int | None = Field(default=None, ge=0)
    availability_24h_pct: float | None = Field(default=None, ge=0, le=100)
    availability_30d_pct: float | None = Field(default=None, ge=0, le=100)
    monitoring_coverage_24h_pct: float | None = Field(default=None, ge=0, le=100)
    monitoring_coverage_30d_pct: float | None = Field(default=None, ge=0, le=100)
    metrics: ExecutiveInstrumentMetrics = Field(default_factory=ExecutiveInstrumentMetrics)
    services: list[ExecutiveInstrumentService] = Field(default_factory=list)
    backup: ExecutiveInstrumentBackup = Field(default_factory=ExecutiveInstrumentBackup)
    integrations: ExecutiveInstrumentIntegration = Field(
        default_factory=ExecutiveInstrumentIntegration
    )
    access: ExecutiveInstrumentAccess = Field(default_factory=ExecutiveInstrumentAccess)
    exchange: ExecutiveInstrumentExchange = Field(default_factory=ExecutiveInstrumentExchange)
    problems: list[ExecutiveInstrumentProblem] = Field(default_factory=list)
    issue: str | None = None
    recommended_action: str | None = None


class ExecutiveInstrumentsSummary(ExecutiveInstrumentStrictModel):
    total_count: int = Field(default=0, ge=0)
    online_count: int = Field(default=0, ge=0)
    critical_count: int = Field(default=0, ge=0)
    warning_count: int = Field(default=0, ge=0)
    not_monitored_count: int = Field(default=0, ge=0)
    backup_gap_count: int = Field(default=0, ge=0)
    access_review_count: int = Field(default=0, ge=0)
    monitoring_coverage_24h_pct: float | None = Field(default=None, ge=0, le=100)


class ExecutiveInstrumentCapabilities(ExecutiveInstrumentStrictModel):
    access_governance: Literal["read_only"] = "read_only"
    access_mutations: Literal[False] = False
    network_scanning: Literal[False] = False


class ExecutiveInstrumentsResponse(ExecutiveInstrumentStrictModel):
    schema_version: Literal[2, 3, 4] = 2
    generated_at: datetime
    source_status: ExecutiveInstrumentSourceStatus
    freshness_status: ExecutiveInstrumentFreshnessStatus
    summary: ExecutiveInstrumentsSummary = Field(default_factory=ExecutiveInstrumentsSummary)
    devices: list[ExecutiveInstrumentDevice] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    capabilities: ExecutiveInstrumentCapabilities = Field(
        default_factory=ExecutiveInstrumentCapabilities
    )
    note: str | None = None

    @model_validator(mode="after")
    def validate_device_grain_and_summary(self) -> ExecutiveInstrumentsResponse:
        device_keys = [device.device_key for device in self.devices]
        if len(device_keys) != len(set(device_keys)):
            raise ValueError("duplicate device_key in infrastructure snapshot")
        for device in self.devices:
            problem_keys = [problem.problem_key for problem in device.problems]
            if len(problem_keys) != len(set(problem_keys)):
                raise ValueError(f"duplicate infrastructure problem_key: {device.device_key}")
            if not device.problems and device.issue:
                device.problems.append(
                    ExecutiveInstrumentProblem(
                        problem_key="configuration:legacy-issue",
                        category=(
                            "monitoring"
                            if device.health_status == "not_monitored"
                            else "configuration"
                        ),
                        severity=("critical" if device.health_status == "critical" else "warning"),
                        title=device.issue,
                        started_at=device.incident_started_at,
                        recommended_action=(
                            device.recommended_action
                            or "Проверить техническую диагностику в управляющем контуре"
                        ),
                    )
                )
        expected = {
            "total_count": len(self.devices),
            "online_count": sum(device.connectivity_status == "online" for device in self.devices),
            "critical_count": sum(device.health_status == "critical" for device in self.devices),
            "warning_count": sum(device.health_status == "warning" for device in self.devices),
            "not_monitored_count": sum(
                device.health_status == "not_monitored" for device in self.devices
            ),
            "backup_gap_count": sum(
                device.backup.status in {"warning", "critical"} for device in self.devices
            ),
            "access_review_count": sum(
                device.access.status == "warning" for device in self.devices
            ),
        }
        for field_name, expected_value in expected.items():
            if getattr(self.summary, field_name) != expected_value:
                raise ValueError(f"infrastructure summary mismatch: {field_name}")
        coverage_values = [
            device.monitoring_coverage_24h_pct
            for device in self.devices
            if device.monitoring_coverage_24h_pct is not None
            and device.health_status not in {"maintenance", "decommissioned"}
        ]
        expected_coverage = (
            round(sum(coverage_values) / len(coverage_values), 1) if coverage_values else None
        )
        if self.summary.monitoring_coverage_24h_pct != expected_coverage:
            raise ValueError("infrastructure summary mismatch: monitoring coverage")

        generated_at = self.generated_at
        if generated_at.tzinfo is None:
            generated_at = generated_at.replace(tzinfo=UTC)
        generated_at_utc = generated_at.astimezone(UTC)
        latest_allowed = generated_at_utc + timedelta(minutes=5)

        def observed_at(value: date | datetime | None) -> datetime | None:
            if value is None:
                return None
            if isinstance(value, datetime):
                return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC)
            return datetime.combine(value, time.min, UTC)

        for device in self.devices:
            exchange_timestamps = [
                device.exchange.last_success_at,
                device.exchange.last_error_at,
            ]
            if any(
                timestamp is not None and timestamp > generated_at_utc
                for value in exchange_timestamps
                if (timestamp := observed_at(value)) is not None
            ):
                raise ValueError("future exchange timestamp in infrastructure snapshot")
            timestamps: list[date | datetime | None] = [
                device.last_attempted_at,
                device.last_success_at,
                device.incident_started_at,
                device.backup.last_backup_at,
                device.backup.last_full_backup_at,
                device.backup.last_differential_backup_at,
                device.backup.last_log_backup_at,
                device.backup.last_restore_test_at,
                device.integrations.last_success_at,
            ]
            for service in device.services:
                timestamps.extend([service.last_verified_at, service.last_success_at])
            if any(
                timestamp is not None and timestamp > latest_allowed
                for value in timestamps
                if (timestamp := observed_at(value)) is not None
            ):
                raise ValueError("future observation timestamp in infrastructure snapshot")
        return self


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
    turnover_method: Literal["net_change_from_snapshots", "gross_cashflow_movements"] = (
        "net_change_from_snapshots"
    )
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
    opening_balance_date: date
    view: ExecutiveManagementBalanceView
    opening_version: int
    closing_version: int
    opening_status: str
    closing_status: str
    opening_validation_error_count: int = 0
    opening_content_sha256: str
    closing_content_sha256: str
    turnover_method: Literal["mixed_gross_cashflow_and_net_change"] = (
        "mixed_gross_cashflow_and_net_change"
    )
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
    available_months: list[str] = Field(default_factory=list)
    available_period_starts: list[str] = Field(default_factory=list)
    available_period_ends: list[str] = Field(default_factory=list)
    selected_month_from: str
    selected_month_to: str
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
