from __future__ import annotations

from datetime import date, datetime
from typing import List, Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class PhoneModel(Base):
    __tablename__ = "phone_models"
    __table_args__ = (
        UniqueConstraint("brand", "model_name", "variant", name="uq_phone_model_identity"),
    )

    brand: Mapped[str] = mapped_column(String(100), index=True)
    brand_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("device_brands.id"), nullable=True, index=True
    )
    model_name: Mapped[str] = mapped_column(String(150), index=True)
    variant: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    announce_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    release_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)

    screen_size_inch: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    screen_technology: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    screen_refresh_rate_hz: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    keywords: Mapped[List[Keyword]] = relationship(
        "Keyword", back_populates="phone_model", cascade="all, delete-orphan"
    )
    aliases: Mapped[List[PhoneModelAlias]] = relationship(
        "PhoneModelAlias",
        back_populates="phone_model",
        cascade="all, delete-orphan",
    )
    product_links: Mapped[List[ProductPhoneModel]] = relationship(
        "ProductPhoneModel",
        back_populates="phone_model",
        cascade="all, delete-orphan",
    )
    competitor_compatibilities: Mapped[List[CompetitorItemCompatibility]] = relationship(
        "CompetitorItemCompatibility",
        back_populates="phone_model",
    )
    device_brand = relationship("DeviceBrand", back_populates="phone_models")


class Keyword(Base):
    __tablename__ = "keywords"
    __table_args__ = (
        UniqueConstraint("phone_model_id", "phrase", name="uq_keyword_phrase_per_model"),
    )

    phrase: Mapped[str] = mapped_column(String(255), index=True)
    language: Mapped[str] = mapped_column(String(10), default="ru")
    category: Mapped[str] = mapped_column(String(50), default="display")
    source: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    phone_model_id: Mapped[int] = mapped_column(ForeignKey("phone_models.id"), index=True)
    phone_model: Mapped[PhoneModel] = relationship("PhoneModel", back_populates="keywords")

    demand_stats: Mapped[List[KeywordDemand]] = relationship(
        "KeywordDemand", back_populates="keyword", cascade="all, delete-orphan"
    )


class KeywordDemand(Base):
    __tablename__ = "keyword_demands"
    __table_args__ = (
        UniqueConstraint("keyword_id", "date", "region", name="uq_keyword_demand_date_region"),
    )

    keyword_id: Mapped[int] = mapped_column(ForeignKey("keywords.id"), index=True)
    date: Mapped[date] = mapped_column(Date)
    region: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    impressions: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    clicks: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    ctr: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    bid_metrics: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    source: Mapped[str] = mapped_column(String(50), default="yandex_direct")
    received_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    keyword: Mapped[Keyword] = relationship("Keyword", back_populates="demand_stats")


class PhoneModelAlias(Base):
    __tablename__ = "phone_model_alias"
    __table_args__ = (
        UniqueConstraint(
            "source",
            "normalized_key",
            "phone_model_id",
            name="uq_phone_model_alias_source_key_model",
        ),
    )

    phone_model_id: Mapped[int] = mapped_column(
        ForeignKey("phone_models.id"), nullable=False, index=True
    )
    source: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    raw_value: Mapped[str] = mapped_column(String(255), nullable=False)
    raw_brand: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    raw_model: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    raw_variant: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    normalized_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    confidence: Mapped[Optional[float]] = mapped_column(Numeric(4, 3), nullable=True)
    is_manual: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    decision_reason: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    device_type: Mapped[str] = mapped_column(String(16), nullable=False, default="other")
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    phone_model: Mapped[PhoneModel] = relationship("PhoneModel", back_populates="aliases")


from app.models.competitor_item_compatibility import CompetitorItemCompatibility  # noqa: E402
from app.models.product_phone_model import ProductPhoneModel  # noqa: E402
