"""Public weekly KPI ingestion contract owned by the management domain."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field


class WeeklyKpiMetricSnapshotIngest(BaseModel):
    metric_code: str
    metric_name: str
    unit: str | None = None
    fact_value: Decimal
    plan_value: Decimal | None = None
    achievement_pct: Decimal | None = None
    bonus_preview_amount: Decimal | None = None
    weight: Decimal | None = None
    previous_fact_value: Decimal | None = None
    delta_abs: Decimal | None = None
    delta_pct: Decimal | None = None
    signal: str | None = None
    sort_order: int = 0
    source_system: str | None = None
    source_entity: str | None = None
    source_as_of: date | None = None
    comment: str | None = None


class WeeklyKpiReportSnapshotIngest(BaseModel):
    report_key: str
    revision: int = Field(ge=1)
    week_start: date
    week_end: date
    employee_key: str
    employee_name: str
    role_code: str | None = None
    position_code: str | None = None
    position_name: str | None = None
    bitrix_user_id: str | None = None
    bitrix_box_user_id: str | None = None
    eligibility_status: Literal["eligible", "quarantine"]
    eligibility_reason: str | None = None
    overall_signal: str | None = None
    summary_payload: dict[str, Any]
    source_as_of: date | None = None
    generated_at: datetime
    metrics: list[WeeklyKpiMetricSnapshotIngest]


class WeeklyKpiSnapshotBatchIngest(BaseModel):
    contract_version: Literal["weekly-kpi-report.v1"] = "weekly-kpi-report.v1"
    generated_at: datetime
    reports: list[WeeklyKpiReportSnapshotIngest]


class WeeklyKpiSnapshotIngestResponse(BaseModel):
    contract_version: str
    inserted: int
    updated: int
    noop: int
    quarantined: int
    replayed: bool = False
