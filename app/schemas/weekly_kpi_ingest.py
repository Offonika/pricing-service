"""Compatibility imports for the management weekly KPI public contract."""

from app.domains.management.contracts import (
    WeeklyKpiMetricSnapshotIngest,
    WeeklyKpiReportSnapshotIngest,
    WeeklyKpiSnapshotBatchIngest,
    WeeklyKpiSnapshotIngestResponse,
)

__all__ = [
    "WeeklyKpiMetricSnapshotIngest",
    "WeeklyKpiReportSnapshotIngest",
    "WeeklyKpiSnapshotBatchIngest",
    "WeeklyKpiSnapshotIngestResponse",
]
