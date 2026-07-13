"""Compatibility imports for management weekly KPI ingestion."""

from app.domains.management.application import (
    WeeklyKpiIdempotencyConflictError,
    ingest_weekly_kpi_snapshots,
)

__all__ = ["WeeklyKpiIdempotencyConflictError", "ingest_weekly_kpi_snapshots"]
