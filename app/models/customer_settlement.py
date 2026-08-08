from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class CustomerSettlementRevision(Base):
    __tablename__ = "customer_settlement_revision"
    __table_args__ = (
        CheckConstraint(
            "status IN ('loading','active','superseded','failed')",
            name="ck_customer_settlement_revision_status",
        ),
        CheckConstraint("currency = 'RUB'", name="ck_customer_settlement_revision_currency"),
        CheckConstraint(
            "expected_row_count >= 0 AND loaded_row_count >= 0 AND zero_row_count >= 0",
            name="ck_customer_settlement_revision_nonnegative_counts",
        ),
        Index(
            "uq_customer_settlement_revision_active",
            "status",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
        Index(
            "ix_customer_settlement_revision_status_created",
            "status",
            "created_at",
        ),
    )

    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="loading",
        server_default="loading",
    )
    organization_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="RUB")
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_db_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_mode: Mapped[str] = mapped_column(String(96), nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expected_row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    loaded_row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    zero_row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[Optional[str]] = mapped_column(String(96), nullable=True)
    error_detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    activated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class CustomerSettlementBalance(Base):
    __tablename__ = "customer_settlement_balance"
    __table_args__ = (
        UniqueConstraint(
            "revision_id",
            "counterparty_ref",
            name="uq_customer_settlement_balance_revision_counterparty",
        ),
        CheckConstraint("currency = 'RUB'", name="ck_customer_settlement_balance_currency"),
        Index(
            "ix_customer_settlement_balance_counterparty_revision",
            "counterparty_ref",
            "revision_id",
        ),
    )

    revision_id: Mapped[int] = mapped_column(
        ForeignKey("customer_settlement_revision.id", ondelete="CASCADE"),
        nullable=False,
    )
    counterparty_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    signed_balance: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="RUB")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CustomerSettlementMappingRevision(Base):
    __tablename__ = "customer_settlement_mapping_revision"
    __table_args__ = (
        CheckConstraint(
            "status IN ('loading','active','superseded','failed')",
            name="ck_customer_settlement_mapping_revision_status",
        ),
        CheckConstraint(
            "expected_entry_count >= 0 AND loaded_entry_count >= 0 AND ambiguous_count >= 0",
            name="ck_customer_settlement_mapping_revision_nonnegative_counts",
        ),
        Index(
            "uq_customer_settlement_mapping_revision_active",
            "status",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
        Index(
            "ix_customer_settlement_mapping_revision_status_created",
            "status",
            "created_at",
        ),
    )

    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="loading",
        server_default="loading",
    )
    source_name: Mapped[str] = mapped_column(String(64), nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    source_checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expected_entry_count: Mapped[int] = mapped_column(Integer, nullable=False)
    loaded_entry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ambiguous_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[Optional[str]] = mapped_column(String(96), nullable=True)
    error_detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    activated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class CustomerSettlementMappingEntry(Base):
    __tablename__ = "customer_settlement_mapping_entry"
    __table_args__ = (
        UniqueConstraint(
            "revision_id",
            "site_user_id",
            name="uq_customer_settlement_mapping_entry_revision_user",
        ),
        CheckConstraint(
            "status IN ('linked','not_linked','ambiguous')",
            name="ck_customer_settlement_mapping_entry_status",
        ),
        Index(
            "ix_customer_settlement_mapping_entry_user_revision",
            "site_user_id",
            "revision_id",
        ),
    )

    revision_id: Mapped[int] = mapped_column(
        ForeignKey("customer_settlement_mapping_revision.id", ondelete="CASCADE"),
        nullable=False,
    )
    site_user_id: Mapped[str] = mapped_column(String(32), nullable=False)
    cluster_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    counterparty_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    source_updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CustomerSettlementPilotAccess(Base):
    __tablename__ = "customer_settlement_pilot_access"
    __table_args__ = (
        UniqueConstraint("site_user_id", name="uq_customer_settlement_pilot_user"),
        Index("ix_customer_settlement_pilot_enabled", "enabled"),
    )

    site_user_id: Mapped[str] = mapped_column(String(32), nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class CustomerSettlementAssertionJti(Base):
    __tablename__ = "customer_settlement_assertion_jti"
    __table_args__ = (
        UniqueConstraint("jti_hash", name="uq_customer_settlement_assertion_jti_hash"),
        Index("ix_customer_settlement_assertion_expires", "expires_at"),
    )

    jti_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
