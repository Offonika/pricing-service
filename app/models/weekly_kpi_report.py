from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class WeeklyKpiReportSnapshot(Base):
    __tablename__ = "weekly_kpi_report_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "report_key", "revision", name="uq_weekly_kpi_report_snapshot_key_revision"
        ),
        Index("ix_weekly_kpi_report_snapshot_week_end", "week_end"),
        Index(
            "ix_weekly_kpi_report_snapshot_lifecycle",
            "week_end",
            "lifecycle_status",
            "eligibility_status",
        ),
        Index("ix_weekly_kpi_report_snapshot_employee_key", "employee_key"),
        Index("ix_weekly_kpi_report_snapshot_bitrix_user_id", "bitrix_user_id"),
        Index("ix_weekly_kpi_report_snapshot_artifact_status", "artifact_status"),
    )

    report_key: Mapped[str] = mapped_column(String(255), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    week_start: Mapped[date] = mapped_column(Date, nullable=False)
    week_end: Mapped[date] = mapped_column(Date, nullable=False)
    employee_key: Mapped[str] = mapped_column(String(128), nullable=False)
    employee_name: Mapped[str] = mapped_column(String(255), nullable=False)
    role_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    position_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    position_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    bitrix_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bitrix_box_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lifecycle_status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    eligibility_status: Mapped[str] = mapped_column(String(32), nullable=False, default="eligible")
    eligibility_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    artifact_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    overall_signal: Mapped[str | None] = mapped_column(String(32), nullable=True)
    summary_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    source_as_of: Mapped[date | None] = mapped_column(Date, nullable=True)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.now(),
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    artifact_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    artifact_sha256: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    metrics: Mapped[list[WeeklyKpiReportMetricSnapshot]] = relationship(
        back_populates="report",
        cascade="all, delete-orphan",
        order_by="WeeklyKpiReportMetricSnapshot.sort_order",
    )


class WeeklyKpiReportMetricSnapshot(Base):
    __tablename__ = "weekly_kpi_report_metric_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "report_id",
            "metric_code",
            name="uq_weekly_kpi_report_metric_snapshot_report_metric",
        ),
        Index("ix_weekly_kpi_report_metric_snapshot_report_id", "report_id"),
        Index("ix_weekly_kpi_report_metric_snapshot_signal", "signal"),
    )

    report_id: Mapped[int] = mapped_column(
        ForeignKey("weekly_kpi_report_snapshot.id", ondelete="CASCADE"),
        nullable=False,
    )
    metric_code: Mapped[str] = mapped_column(String(128), nullable=False)
    metric_name: Mapped[str] = mapped_column(String(255), nullable=False)
    unit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    fact_value: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False, default=0)
    plan_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    achievement_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    bonus_preview_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    weight: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    previous_fact_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    delta_abs: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    delta_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    signal: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_system: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_entity: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_as_of: Mapped[date | None] = mapped_column(Date, nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    report: Mapped[WeeklyKpiReportSnapshot] = relationship(back_populates="metrics")


class WeeklyKpiIngestRequest(Base):
    __tablename__ = "weekly_kpi_ingest_request"

    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    result_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.now(),
    )
