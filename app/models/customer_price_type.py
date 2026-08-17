"""Durable models for the customer price-type management contour."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class CustomerPriceTypeProfile(Base):
    __tablename__ = "customer_price_type_profile"
    __table_args__ = (
        UniqueConstraint("counterparty_ref", name="uq_customer_price_type_profile_ref"),
        CheckConstraint(
            "counterparty_ref = lower(counterparty_ref)",
            name="ck_customer_price_type_profile_ref_lower",
        ),
        Index("ix_customer_price_type_profile_department", "department_ref"),
        Index("ix_customer_price_type_profile_owner", "owner_ref"),
    )

    counterparty_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    counterparty_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    counterparty_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    department_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    department_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    owner_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    owner_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_service_card: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_hygiene: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    master_data_flags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    latest_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "customer_price_type_snapshot.id",
            name="fk_customer_price_type_profile_latest_snapshot",
            use_alter=True,
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    open_case_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "customer_price_type_case.id",
            name="fk_customer_price_type_profile_open_case",
            use_alter=True,
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class CustomerPriceTypeRun(Base):
    __tablename__ = "customer_price_type_run"
    __table_args__ = (
        UniqueConstraint("run_key", name="uq_customer_price_type_run_key"),
        CheckConstraint(
            "status IN ('started','completed','partial','failed')",
            name="ck_customer_price_type_run_status",
        ),
        Index("ix_customer_price_type_run_month_status", "snapshot_month", "status"),
        Index("ix_customer_price_type_run_fingerprint", "source_fingerprint"),
        Index("ix_customer_price_type_run_started", "started_at"),
    )

    run_key: Mapped[str] = mapped_column(String(255), nullable=False)
    snapshot_month: Mapped[date] = mapped_column(Date, nullable=False)
    ruleset_version: Mapped[str] = mapped_column(String(64), nullable=False)
    as_of: Mapped[date] = mapped_column(Date, nullable=False)
    window_start: Mapped[date] = mapped_column(Date, nullable=False)
    window_end: Mapped[date] = mapped_column(Date, nullable=False)
    source_statuses: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False, default=dict)
    source_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    input_count: Mapped[int] = mapped_column(nullable=False, default=0)
    excluded_count: Mapped[int] = mapped_column(nullable=False, default=0)
    calculated_count: Mapped[int] = mapped_column(nullable=False, default=0)
    conflict_count: Mapped[int] = mapped_column(nullable=False, default=0)
    actionable_count: Mapped[int] = mapped_column(nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="started")
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class CustomerPriceTypeSnapshot(Base):
    __tablename__ = "customer_price_type_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "run_id", "profile_id", name="uq_customer_price_type_snapshot_run_profile"
        ),
        CheckConstraint(
            "source_status IN ('ready','partial','conflict','excluded')",
            name="ck_customer_price_type_snapshot_source_status",
        ),
        Index("ix_customer_price_type_snapshot_profile_month", "profile_id", "snapshot_month"),
        Index("ix_customer_price_type_snapshot_month_action", "snapshot_month", "action_required"),
        Index("ix_customer_price_type_snapshot_hash", "snapshot_hash"),
        Index("ix_customer_price_type_snapshot_source", "source_status"),
    )

    run_id: Mapped[int] = mapped_column(
        ForeignKey("customer_price_type_run.id", ondelete="CASCADE"), nullable=False
    )
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("customer_price_type_profile.id", ondelete="CASCADE"), nullable=False
    )
    counterparty_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_month: Mapped[date] = mapped_column(Date, nullable=False)
    ruleset_version: Mapped[str] = mapped_column(String(64), nullable=False)
    current_price_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    current_level: Mapped[str | None] = mapped_column(String(32), nullable=True)
    price_type_variant: Mapped[str | None] = mapped_column(String(32), nullable=True)
    contract_candidates: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    monthly_sales: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False, default=dict)
    total_3m: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    last_month: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    economics: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    payments: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    returns: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    history: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    source_status: Mapped[str] = mapped_column(String(32), nullable=False)
    source_statuses: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False, default=dict)
    conflicts: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    stop_factors: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    system_recommendation: Mapped[str] = mapped_column(String(128), nullable=False)
    recommended_price_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    recommendation_reason: Mapped[str] = mapped_column(Text, nullable=False)
    action_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    case_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    review_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reasons: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )


class CustomerPriceTypeCase(Base):
    __tablename__ = "customer_price_type_case"
    __table_args__ = (
        UniqueConstraint("case_key", name="uq_customer_price_type_case_key"),
        UniqueConstraint(
            "profile_id", "snapshot_month", name="uq_customer_price_type_case_profile_month"
        ),
        CheckConstraint(
            "stage IN ('NEW','MANAGER_WORK','ISOLATE','DATA_CHECK','SPECIAL_REVIEW',"
            "'DOWNGRADE_APPROVAL','READY_FOR_1C','CLOSED_KEEP','CLOSED_CHANGED','ONEC_ERROR')",
            name="ck_customer_price_type_case_stage",
        ),
        CheckConstraint(
            "case_type IN ('manager_work','isolate','recovery','data_check',"
            "'special_review','downgrade_approval')",
            name="ck_customer_price_type_case_type",
        ),
        CheckConstraint(
            "approval_status IN ('not_requested','pending','approved','rejected','stale')",
            name="ck_customer_price_type_case_approval_status",
        ),
        CheckConstraint(
            "onec_export_status IN ('not_ready','blocked','ready','exported','error')",
            name="ck_customer_price_type_case_onec_export_status",
        ),
        CheckConstraint(
            "onec_readback_status IN ('not_requested','pending','confirmed','mismatch','error')",
            name="ck_customer_price_type_case_onec_readback_status",
        ),
        CheckConstraint("version > 0", name="ck_customer_price_type_case_version"),
        Index("ix_customer_price_type_case_month_stage", "snapshot_month", "stage"),
        Index("ix_customer_price_type_case_worklist", "snapshot_month", "case_type", "stage"),
        Index("ix_customer_price_type_case_department", "department_ref", "stage"),
        Index("ix_customer_price_type_case_owner", "owner_ref", "stage"),
        Index("ix_customer_price_type_case_review", "review_type", "stage"),
    )

    case_key: Mapped[str] = mapped_column(String(96), nullable=False)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("customer_price_type_profile.id", ondelete="CASCADE"), nullable=False
    )
    current_snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("customer_price_type_snapshot.id", ondelete="RESTRICT"), nullable=False
    )
    snapshot_month: Mapped[date] = mapped_column(Date, nullable=False)
    ruleset_version: Mapped[str] = mapped_column(String(64), nullable=False)
    case_type: Mapped[str] = mapped_column(String(64), nullable=False)
    review_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reasons: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    stage: Mapped[str] = mapped_column(String(32), nullable=False, default="NEW")
    owner_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    owner_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    department_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    department_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    manager_action_completeness: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    system_recommendation: Mapped[str] = mapped_column(String(128), nullable=False)
    recommended_price_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    human_final_decision: Mapped[str | None] = mapped_column(String(128), nullable=True)
    approval_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="not_requested"
    )
    approver_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approver_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    approved_snapshot_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bitrix_entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bitrix_sync_version: Mapped[int | None] = mapped_column(nullable=True)
    onec_export_status: Mapped[str] = mapped_column(String(32), nullable=False, default="not_ready")
    onec_readback_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="not_requested"
    )
    version: Mapped[int] = mapped_column(nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class CustomerPriceTypeCaseEvent(Base):
    __tablename__ = "customer_price_type_case_event"
    __table_args__ = (
        UniqueConstraint(
            "case_id", "idempotency_key", name="uq_customer_price_type_case_event_key"
        ),
        Index("ix_customer_price_type_case_event_case_at", "case_id", "event_at"),
        CheckConstraint(
            "source IN ('calculation','app','bitrix','onec','system')",
            name="ck_customer_price_type_case_event_source",
        ),
    )

    case_id: Mapped[int] = mapped_column(
        ForeignKey("customer_price_type_case.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    event_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    actor: Mapped[str] = mapped_column(String(255), nullable=False, default="system")
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="calculation")
    before_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    after_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, nullable=False, default=dict
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)


class CustomerPriceTypeReviewBatch(Base):
    __tablename__ = "customer_price_type_review_batch"
    __table_args__ = (
        UniqueConstraint("batch_key", name="uq_customer_price_type_review_batch_key"),
        CheckConstraint(
            "status IN ('ready','superseded')",
            name="ck_customer_price_type_review_batch_status",
        ),
        Index("ix_customer_price_type_review_batch_status", "status"),
    )

    batch_key: Mapped[str] = mapped_column(String(128), nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_files: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    expected_counts: Mapped[dict[str, int]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="ready")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class CustomerPriceTypeReviewBatchItem(Base):
    __tablename__ = "customer_price_type_review_batch_item"
    __table_args__ = (
        UniqueConstraint(
            "batch_id",
            "counterparty_ref",
            name="uq_customer_price_type_review_batch_item_ref",
        ),
        UniqueConstraint(
            "batch_id",
            "counterparty_code",
            name="uq_customer_price_type_review_batch_item_code",
        ),
        CheckConstraint(
            "expected_bucket IN ('working_bronze','review_queue')",
            name="ck_customer_price_type_review_batch_item_bucket",
        ),
        CheckConstraint(
            "counterparty_ref = lower(counterparty_ref)",
            name="ck_customer_price_type_review_batch_item_ref_lower",
        ),
        Index(
            "ix_customer_price_type_review_batch_item_bucket",
            "batch_id",
            "expected_bucket",
        ),
    )

    batch_id: Mapped[int] = mapped_column(
        ForeignKey("customer_price_type_review_batch.id", ondelete="CASCADE"),
        nullable=False,
    )
    counterparty_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    counterparty_code: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_bucket: Mapped[str] = mapped_column(String(32), nullable=False)
    expected_price_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_row: Mapped[int] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )


class CustomerPriceTypeQualitySample(Base):
    __tablename__ = "customer_price_type_quality_sample"
    __table_args__ = (
        UniqueConstraint("snapshot_id", name="uq_customer_price_type_quality_sample_snapshot"),
        CheckConstraint(
            "system_group IN ('manager_work','isolate','recovery','data_check',"
            "'special_review','downgrade_approval','no_action')",
            name="ck_customer_price_type_quality_sample_system_group",
        ),
        CheckConstraint(
            "correct_group IS NULL OR correct_group IN "
            "('manager_work','isolate','recovery','data_check','special_review',"
            "'downgrade_approval','no_action')",
            name="ck_customer_price_type_quality_sample_correct_group",
        ),
        CheckConstraint(
            "status IN ('pending','reviewed')",
            name="ck_customer_price_type_quality_sample_status",
        ),
        CheckConstraint("version > 0", name="ck_customer_price_type_quality_sample_version"),
        Index(
            "ix_customer_price_type_quality_sample_run_status",
            "run_id",
            "status",
        ),
        Index(
            "ix_customer_price_type_quality_sample_run_group",
            "run_id",
            "system_group",
        ),
    )

    run_id: Mapped[int] = mapped_column(
        ForeignKey("customer_price_type_run.id", ondelete="CASCADE"), nullable=False
    )
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("customer_price_type_snapshot.id", ondelete="CASCADE"), nullable=False
    )
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("customer_price_type_profile.id", ondelete="CASCADE"), nullable=False
    )
    system_group: Mapped[str] = mapped_column(String(64), nullable=False)
    correct_group: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    selected_by: Mapped[str] = mapped_column(String(255), nullable=False)
    selected_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    reviewed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
