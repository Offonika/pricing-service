from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

_JSON = JSON().with_variant(JSONB, "postgresql")


class DisplayFamilyRegistryVersion(Base):
    """Immutable family membership snapshot with a switchable lifecycle status."""

    __tablename__ = "display_family_registry_version"
    __table_args__ = (
        UniqueConstraint("version_number", name="uq_display_family_registry_version_number"),
        UniqueConstraint(
            "inventory_checksum", name="uq_display_family_registry_inventory_checksum"
        ),
        CheckConstraint(
            "status IN ('active', 'superseded', 'rolled_back')",
            name="ck_display_family_registry_version_status",
        ),
        Index(
            "uq_display_family_registry_single_active",
            "status",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
    )

    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    source_schema: Mapped[str] = mapped_column(String(100), nullable=False)
    source_bundle_path: Mapped[str] = mapped_column(String(512), nullable=False)
    inventory_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    membership_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    inventory_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    inventory_csv_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    report_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_quality_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_family_count: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_member_count: Mapped[int] = mapped_column(Integer, nullable=False)
    actual_family_count: Mapped[int] = mapped_column(Integer, nullable=False)
    actual_member_count: Mapped[int] = mapped_column(Integer, nullable=False)
    source_manifest_json: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    source_summary_json: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    evidence_snapshot_json: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    created_by: Mapped[str] = mapped_column(String(160), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    families: Mapped[list[DisplayFamily]] = relationship(
        "DisplayFamily",
        back_populates="registry_version",
        cascade="all, delete-orphan",
    )
    members: Mapped[list[DisplayFamilyMember]] = relationship(
        "DisplayFamilyMember",
        back_populates="registry_version",
        cascade="all, delete-orphan",
    )
    events: Mapped[list[DisplayFamilyDecisionEvent]] = relationship(
        "DisplayFamilyDecisionEvent",
        back_populates="registry_version",
        cascade="all, delete-orphan",
    )


class DisplayFamily(Base):
    __tablename__ = "display_family"
    __table_args__ = (
        UniqueConstraint(
            "registry_version_id",
            "family_key",
            name="uq_display_family_version_key",
        ),
        Index("ix_display_family_version_singleton", "registry_version_id", "is_singleton"),
        Index("ix_display_family_version_review", "registry_version_id", "review_member_count"),
    )

    registry_version_id: Mapped[int] = mapped_column(
        ForeignKey("display_family_registry_version.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    family_key: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    member_count: Mapped[int] = mapped_column(Integer, nullable=False)
    is_singleton: Mapped[bool] = mapped_column(Boolean, nullable=False)
    total_current_stock_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    review_member_count: Mapped[int] = mapped_column(Integer, nullable=False)
    matching_review_member_count: Mapped[int] = mapped_column(Integer, nullable=False)
    quality_unknown_member_count: Mapped[int] = mapped_column(Integer, nullable=False)
    construction_unknown_member_count: Mapped[int] = mapped_column(Integer, nullable=False)
    phone_model_ids_json: Mapped[list[int]] = mapped_column(_JSON, nullable=False)
    phone_models_json: Mapped[list[dict[str, Any]]] = mapped_column(_JSON, nullable=False)
    physical_model_signatures_json: Mapped[list[list[str]]] = mapped_column(_JSON, nullable=False)
    segment_ids_json: Mapped[list[str]] = mapped_column(_JSON, nullable=False)
    warning_codes_json: Mapped[list[str]] = mapped_column(_JSON, nullable=False)
    note_codes_json: Mapped[list[str]] = mapped_column(_JSON, nullable=False)
    evidence_snapshot_json: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    registry_version: Mapped[DisplayFamilyRegistryVersion] = relationship(
        "DisplayFamilyRegistryVersion", back_populates="families"
    )
    members: Mapped[list[DisplayFamilyMember]] = relationship(
        "DisplayFamilyMember",
        back_populates="family",
        cascade="all, delete-orphan",
        order_by="DisplayFamilyMember.id",
    )


class DisplayFamilyMember(Base):
    __tablename__ = "display_family_member"
    __table_args__ = (
        UniqueConstraint(
            "registry_version_id",
            "product_id",
            name="uq_display_family_member_version_product",
        ),
        Index("ix_display_family_member_family_segment", "family_id", "segment_id"),
    )

    registry_version_id: Mapped[int] = mapped_column(
        ForeignKey("display_family_registry_version.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    family_id: Mapped[int] = mapped_column(
        ForeignKey("display_family.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[int] = mapped_column(
        ForeignKey("product.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    segment_id: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    proposal_status: Mapped[str] = mapped_column(String(64), nullable=False)
    quality_segment: Mapped[str] = mapped_column(String(64), nullable=False)
    construction_segment: Mapped[str] = mapped_column(String(64), nullable=False)
    requires_manual_review: Mapped[bool] = mapped_column(Boolean, nullable=False)
    current_stock_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    warning_codes_json: Mapped[list[str]] = mapped_column(_JSON, nullable=False)
    note_codes_json: Mapped[list[str]] = mapped_column(_JSON, nullable=False)
    scope_reasons_json: Mapped[list[str]] = mapped_column(_JSON, nullable=False)
    product_snapshot_json: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    matching_evidence_json: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    identity_evidence_json: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    evidence_snapshot_json: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    registry_version: Mapped[DisplayFamilyRegistryVersion] = relationship(
        "DisplayFamilyRegistryVersion", back_populates="members"
    )
    family: Mapped[DisplayFamily] = relationship("DisplayFamily", back_populates="members")
    product = relationship("Product")


class DisplayFamilyDecisionEvent(Base):
    """Append-only audit event for bootstrap and future manual family decisions."""

    __tablename__ = "display_family_decision_event"
    __table_args__ = (
        Index("ix_display_family_event_version_created", "registry_version_id", "created_at"),
        Index("ix_display_family_event_family_created", "family_id", "created_at"),
    )

    registry_version_id: Mapped[int] = mapped_column(
        ForeignKey("display_family_registry_version.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    family_id: Mapped[int | None] = mapped_column(
        ForeignKey("display_family.id", ondelete="SET NULL"), nullable=True, index=True
    )
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("product.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(160), nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    effective_at: Mapped[date] = mapped_column(Date, nullable=False)
    evidence_snapshot_json: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    registry_version: Mapped[DisplayFamilyRegistryVersion] = relationship(
        "DisplayFamilyRegistryVersion", back_populates="events"
    )
    family: Mapped[DisplayFamily | None] = relationship("DisplayFamily")
    product = relationship("Product")
