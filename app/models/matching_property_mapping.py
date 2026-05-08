from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class MatchingPropertyProfile(Base):
    __tablename__ = "matching_property_profile"
    __table_args__ = (UniqueConstraint("code", name="uq_matching_property_profile_code"),)

    code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    item_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    rules = relationship(
        "MatchingPropertyRule",
        back_populates="profile",
        cascade="all, delete-orphan",
        order_by="MatchingPropertyRule.sort_order",
    )


class MatchingPropertyRule(Base):
    __tablename__ = "matching_property_rule"
    __table_args__ = (
        UniqueConstraint(
            "profile_id",
            "property_key",
            name="uq_matching_property_rule_profile_key",
        ),
    )

    profile_id: Mapped[int] = mapped_column(
        ForeignKey("matching_property_profile.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    property_key: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    product_field: Mapped[str] = mapped_column(String(128), nullable=False)
    competitor_field: Mapped[str] = mapped_column(String(128), nullable=False)
    comparison_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="exact")
    severity: Mapped[str] = mapped_column(String(32), nullable=False, default="review")
    config_json: Mapped[Optional[dict]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    profile = relationship("MatchingPropertyProfile", back_populates="rules")
    value_maps = relationship(
        "MatchingPropertyValueMap",
        back_populates="rule",
        cascade="all, delete-orphan",
        order_by="MatchingPropertyValueMap.id",
    )
    audit_events = relationship("MatchingPropertyRuleAudit", back_populates="rule")


class MatchingPropertyValueMap(Base):
    __tablename__ = "matching_property_value_map"
    __table_args__ = (
        UniqueConstraint(
            "rule_id",
            "competitor_source",
            "competitor_value",
            name="uq_matching_property_value_map_rule_source_value",
        ),
    )

    rule_id: Mapped[int] = mapped_column(
        ForeignKey("matching_property_rule.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    competitor_source: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    competitor_value: Mapped[str] = mapped_column(String(255), nullable=False)
    mapped_value: Mapped[str] = mapped_column(String(255), nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    rule = relationship("MatchingPropertyRule", back_populates="value_maps")


class MatchingPropertyRuleAudit(Base):
    __tablename__ = "matching_property_rule_audit"

    rule_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("matching_property_rule.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    actor: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    before_json: Mapped[Optional[dict]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=True
    )
    after_json: Mapped[Optional[dict]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=True
    )
    comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    rule = relationship("MatchingPropertyRule", back_populates="audit_events")
