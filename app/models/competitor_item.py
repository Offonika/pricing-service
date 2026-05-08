from __future__ import annotations

import enum
import os
from datetime import datetime
from typing import List, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class CompetitorItemParseStatus(str, enum.Enum):  # noqa: UP042
    OK = "ok"
    INVALID_JSON = "invalid_json"
    TIMEOUT = "timeout"
    LOW_CONFIDENCE = "low_confidence"
    CONFLICT = "conflict"


class CompetitorItem(Base):
    """
    Каталог товаров конкурентов (уникальность по competitor + external_id).
    Хранит нормализованное название, parsed_* и последнюю цену/наличие.
    """

    __tablename__ = "competitor_item"
    __table_args__ = (
        UniqueConstraint(
            "competitor", "external_id", name="uq_competitor_item_competitor_external"
        ),
    )

    competitor: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    product_model: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    category: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    category_group: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)

    name_norm: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    sku_norm: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)

    item_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    normalized_title: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    attrs_json: Mapped[Optional[dict]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=True
    )
    item_brand: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    attrs_model: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    attrs_variant: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    attrs_color: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    attrs_capacity: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    attrs_size_inch: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    attrs_type: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    attrs_quality: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    attrs_construction: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    attrs_refresh_rate_hz: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    screen_matrix_type: Mapped[Optional[str]] = mapped_column(
        "Тип дисплея", String(32), nullable=True, quote=True
    )
    screen_kit: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    backlight: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    screen_construction: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    screen_quality_grade: Mapped[Optional[str]] = mapped_column(
        "Качество", String(32), nullable=True, quote=True
    )
    refresh_rate_hz: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    oleophobic: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    has_frame: Mapped[Optional[bool]] = mapped_column("В_рамке", Boolean, nullable=True, quote=True)
    has_touch: Mapped[Optional[bool]] = mapped_column(
        "С тачскрином", Boolean, nullable=True, quote=True
    )
    has_ic_pad: Mapped[Optional[bool]] = mapped_column(
        "Площадка под IC", Boolean, nullable=True, quote=True
    )
    has_binding_no_solder: Mapped[Optional[bool]] = mapped_column(
        "Привязка без пайки", Boolean, nullable=True, quote=True
    )
    item_manufacturer: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    matrix_tags: Mapped[Optional[List[str]]] = mapped_column(
        "Теги_Матрицы", JSON().with_variant(JSONB, "postgresql"), nullable=True, quote=True
    )
    color: Mapped[Optional[str]] = mapped_column("Цвет", String(64), nullable=True, quote=True)
    notes_raw_tokens: Mapped[Optional[List[str]]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=True
    )
    llm_confidence: Mapped[Optional[float]] = mapped_column(Numeric(4, 3), nullable=True)
    llm_raw_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    parse_status: Mapped[Optional[CompetitorItemParseStatus]] = mapped_column(
        Enum(
            CompetitorItemParseStatus,
            name="competitor_item_parse_status",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=True,
    )
    parse_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    llm_model: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    prompt_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    parse_version: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    price_opt: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)
    price_roz: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)
    availability: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    scraped_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    first_seen_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    parsed_device_brand: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True, index=True
    )
    parsed_device_model: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    parsed_device_variant: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    parse_confidence: Mapped[Optional[float]] = mapped_column(Numeric(4, 3), nullable=True)
    parse_notes: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    snapshots = relationship(
        "CompetitorItemSnapshot",
        back_populates="item",
        cascade="all, delete-orphan",
    )
    if not os.getenv("COMPETITOR_ITEM_SKIP_REL"):
        match = relationship(
            "CompetitorItemMatch",
            back_populates="competitor_item",
            cascade="all, delete-orphan",
            uselist=False,
        )
        compatibilities = relationship(
            "CompetitorItemCompatibility",
            back_populates="item",
            cascade="all, delete-orphan",
        )


class CompetitorItemSnapshot(Base):
    """
    Исторические срезы цен/наличия для конкурента.
    """

    __tablename__ = "competitor_item_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "competitor_item_id",
            "scraped_at",
            name="uq_competitor_item_snapshot_item_scraped_at",
        ),
    )

    competitor_item_id: Mapped[int] = mapped_column(
        ForeignKey("competitor_item.id"), nullable=False, index=True
    )
    price_roz: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)
    price_opt: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)
    availability: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    scraped_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )

    item = relationship("CompetitorItem", back_populates="snapshots")
