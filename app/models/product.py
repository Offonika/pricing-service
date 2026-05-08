from __future__ import annotations

from typing import List, Optional

from sqlalchemy import JSON, Boolean, Float, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.competitor_price import CompetitorPrice
from app.models.product_compatibility import ProductCompatibility
from app.models.product_match import ProductMatch
from app.models.product_phone_model import ProductPhoneModel
from app.models.product_sku_plan import ProductSkuPlan
from app.models.product_stock import ProductStock


class Product(Base):
    article: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    fact_sku: Mapped[Optional[str]] = mapped_column(String(35), unique=True, index=True)
    planned_sku: Mapped[Optional[str]] = mapped_column(String(35), index=True)
    sku_sync_status: Mapped[Optional[str]] = mapped_column(String(32))
    sku_sync_error: Mapped[Optional[str]] = mapped_column(String(255))
    code_1c: Mapped[Optional[str]] = mapped_column(String(100), index=True)
    info_system_code: Mapped[Optional[str]] = mapped_column(String(100), index=True)
    name: Mapped[str] = mapped_column(String(255))
    brand: Mapped[Optional[str]] = mapped_column(String(100))
    category: Mapped[Optional[str]] = mapped_column(String(100))
    subject: Mapped[Optional[str]] = mapped_column(String(100))
    subject_1c: Mapped[Optional[str]] = mapped_column(String(100))
    subject_generated: Mapped[Optional[str]] = mapped_column(String(100))
    subject_source: Mapped[Optional[str]] = mapped_column(String(32))
    quality_raw: Mapped[Optional[str]] = mapped_column(String(100))
    display_quality_raw: Mapped[Optional[str]] = mapped_column(String(100))
    quality: Mapped[Optional[str]] = mapped_column(String(100))
    display_type: Mapped[Optional[str]] = mapped_column(String(100))
    display_quality: Mapped[Optional[str]] = mapped_column(String(100))
    display_construction: Mapped[Optional[str]] = mapped_column(String(50))
    display_refresh_rate_hz: Mapped[Optional[int]] = mapped_column(Integer)
    display_screen_kit: Mapped[Optional[str]] = mapped_column(String(32))
    display_has_frame: Mapped[Optional[bool]] = mapped_column(Boolean)
    display_has_touch: Mapped[Optional[bool]] = mapped_column(Boolean)
    display_has_ic_pad: Mapped[Optional[bool]] = mapped_column(Boolean)
    display_has_binding_no_solder: Mapped[Optional[bool]] = mapped_column(Boolean)
    display_backlight: Mapped[Optional[str]] = mapped_column(String(32))
    display_matrix_tags: Mapped[Optional[List[str]]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql")
    )
    display_modification_status: Mapped[Optional[str]] = mapped_column(String(32))
    display_modification_source: Mapped[Optional[str]] = mapped_column(String(32))
    display_modification_confidence: Mapped[Optional[float]] = mapped_column(Float)
    display_parse_version: Mapped[Optional[str]] = mapped_column(String(50))
    display_diagonal: Mapped[Optional[str]] = mapped_column(String(50))
    display_resolution: Mapped[Optional[str]] = mapped_column(String(50))
    in_frame: Mapped[Optional[str]] = mapped_column(String(50))
    manufacturer: Mapped[Optional[str]] = mapped_column(String(100))
    color: Mapped[Optional[str]] = mapped_column(String(100))
    vid_nomenklatury: Mapped[Optional[str]] = mapped_column("Вид_номенклатуры", String(128))
    vid_nomenklatury_1c: Mapped[Optional[str]] = mapped_column(String(128))
    vid_nomenklatury_generated: Mapped[Optional[str]] = mapped_column(String(128))
    vid_nomenklatury_source: Mapped[Optional[str]] = mapped_column(String(32))
    battery_capacity_mah: Mapped[Optional[int]] = mapped_column(Integer)
    battery_is_high_capacity: Mapped[Optional[bool]] = mapped_column(Boolean)
    battery_voltage: Mapped[Optional[str]] = mapped_column(String(32))
    battery_energy_wh: Mapped[Optional[str]] = mapped_column(String(32))
    cable_connector_input: Mapped[Optional[str]] = mapped_column(String(50))
    cable_connector_output: Mapped[Optional[str]] = mapped_column(String(50))
    cable_length: Mapped[Optional[str]] = mapped_column(String(50))
    charger_power_w: Mapped[Optional[int]] = mapped_column(Integer)
    charger_technology: Mapped[Optional[str]] = mapped_column(String(50))
    charger_plug_type: Mapped[Optional[str]] = mapped_column(String(50))
    camera_position: Mapped[Optional[str]] = mapped_column(String(20))
    camera_megapixels: Mapped[Optional[int]] = mapped_column(Integer)
    flex_purpose: Mapped[Optional[str]] = mapped_column(String(100))
    glass_type: Mapped[Optional[str]] = mapped_column(String(50))
    glass_form: Mapped[Optional[str]] = mapped_column(String(50))
    chip_code: Mapped[Optional[str]] = mapped_column(String(100))
    part_type: Mapped[Optional[str]] = mapped_column(String(100))
    set_composition: Mapped[Optional[str]] = mapped_column(String(255))
    set_quantity: Mapped[Optional[int]] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(default=True)
    is_marked_for_deletion: Mapped[bool] = mapped_column(default=False)

    competitor_prices: Mapped[List[CompetitorPrice]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )
    stock: Mapped[Optional[ProductStock]] = relationship(
        "ProductStock", back_populates="product", cascade="all, delete-orphan", uselist=False
    )
    matches: Mapped[List[ProductMatch]] = relationship(
        "ProductMatch", back_populates="product", cascade="all, delete-orphan"
    )
    compatibilities: Mapped[List[ProductCompatibility]] = relationship(
        "ProductCompatibility",
        back_populates="product",
        cascade="all, delete-orphan",
    )
    phone_model_links: Mapped[List[ProductPhoneModel]] = relationship(
        "ProductPhoneModel",
        back_populates="product",
        cascade="all, delete-orphan",
    )
    sku_plans: Mapped[List[ProductSkuPlan]] = relationship(
        "ProductSkuPlan",
        back_populates="product",
        cascade="all, delete-orphan",
        order_by="ProductSkuPlan.id.desc()",
    )
