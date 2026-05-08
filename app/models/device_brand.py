from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class DeviceBrand(Base):
    __tablename__ = "device_brands"
    __table_args__ = (
        UniqueConstraint("code", name="uq_device_brand_code"),
        UniqueConstraint("name", name="uq_device_brand_name"),
    )

    code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    group_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    aliases = relationship(
        "DeviceBrandAlias",
        back_populates="brand",
        cascade="all, delete-orphan",
    )
    phone_models = relationship("PhoneModel", back_populates="device_brand")
    competitor_compatibilities = relationship(
        "CompetitorItemCompatibility",
        back_populates="device_brand_ref",
    )


class DeviceBrandAlias(Base):
    __tablename__ = "device_brand_aliases"
    __table_args__ = (
        UniqueConstraint(
            "source",
            "normalized_key",
            "brand_id",
            name="uq_device_brand_alias_source_key_brand",
        ),
    )

    brand_id: Mapped[int] = mapped_column(
        ForeignKey("device_brands.id"), nullable=False, index=True
    )
    source: Mapped[str] = mapped_column(String(50), nullable=False, default="manual", index=True)
    raw_value: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    confidence: Mapped[Optional[float]] = mapped_column(Numeric(4, 3), nullable=True)
    is_manual: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    decision_reason: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    brand = relationship("DeviceBrand", back_populates="aliases")


class CompatibilityMappingDecision(Base):
    __tablename__ = "compatibility_mapping_decisions"

    entity_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    entity_id: Mapped[int] = mapped_column(nullable=False, index=True)
    source: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, index=True)
    raw_value: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    brand_id: Mapped[Optional[int]] = mapped_column(ForeignKey("device_brands.id"), nullable=True)
    phone_model_ids_json: Mapped[Optional[list[int]]] = mapped_column(JSON, nullable=True)
    actor: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    brand = relationship("DeviceBrand")
