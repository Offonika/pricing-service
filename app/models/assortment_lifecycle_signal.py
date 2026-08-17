from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    event,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

_JSON = JSON().with_variant(JSONB(), "postgresql")


class AssortmentLifecycleSignal(Base):
    """Immutable point-in-time signal known to the procurement calculation."""

    __tablename__ = "assortment_lifecycle_signal"
    __table_args__ = (
        UniqueConstraint("signal_key", name="uq_assortment_lifecycle_signal_key"),
        CheckConstraint(
            "reliability >= 0 AND reliability <= 1",
            name="ck_assortment_lifecycle_signal_reliability",
        ),
        CheckConstraint(
            "schema_version = 'assortment_signal.v1'",
            name="ck_assortment_lifecycle_signal_schema_version",
        ),
        CheckConstraint(
            "signal_type IN ('customer_sale', 'stock_availability', "
            "'supplier_order', 'supplier_receipt', 'cargo', 'kmp4', "
            "'site_order', 'site_cart', 'wordstat_direction')",
            name="ck_assortment_lifecycle_signal_type",
        ),
        CheckConstraint(
            "available_at >= occurred_at",
            name="ck_assortment_lifecycle_signal_available_after_occurrence",
        ),
        CheckConstraint(
            "nomenclature_code IS NOT NULL OR display_family_key IS NOT NULL",
            name="ck_assortment_lifecycle_signal_linkage",
        ),
        CheckConstraint(
            "(display_family_key IS NULL AND display_family_registry_version IS NULL) "
            "OR (display_family_key IS NOT NULL "
            "AND display_family_registry_version IS NOT NULL)",
            name="ck_assortment_lifecycle_signal_family_version",
        ),
        CheckConstraint(
            "display_family_registry_version IS NULL " "OR display_family_registry_version > 0",
            name="ck_assortment_lifecycle_signal_family_version_positive",
        ),
        CheckConstraint(
            "quantity IS NULL OR quantity >= 0",
            name="ck_assortment_lifecycle_signal_quantity",
        ),
        CheckConstraint(
            "direction IS NULL OR direction IN ('up', 'down', 'flat', 'unknown')",
            name="ck_assortment_lifecycle_signal_direction",
        ),
        CheckConstraint(
            "signal_type <> 'wordstat_direction' "
            "OR (quantity IS NULL AND direction IS NOT NULL)",
            name="ck_assortment_lifecycle_signal_wordstat_direction_only",
        ),
        Index(
            "ix_assortment_lifecycle_signal_available_type",
            "available_at",
            "signal_type",
        ),
        Index(
            "ix_assortment_lifecycle_signal_sku_available",
            "nomenclature_code",
            "available_at",
        ),
        Index(
            "ix_assortment_lifecycle_signal_family_available",
            "display_family_key",
            "available_at",
        ),
        Index(
            "ix_assortment_lifecycle_signal_source_event",
            "source",
            "signal_type",
            "source_event_id",
        ),
    )

    schema_version: Mapped[str] = mapped_column(
        String(64), nullable=False, default="assortment_signal.v1"
    )
    signal_key: Mapped[str] = mapped_column(String(64), nullable=False)
    signal_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    source_event_id: Mapped[str] = mapped_column(String(255), nullable=False)

    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    nomenclature_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    display_family_key: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    display_family_registry_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    reliability: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    reliability_reason: Mapped[str] = mapped_column(String(255), nullable=False)
    quantity: Mapped[Optional[Decimal]] = mapped_column(Numeric(28, 3), nullable=True)
    direction: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)

    payload: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False, default=dict)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


@event.listens_for(AssortmentLifecycleSignal, "before_update")
def _reject_signal_update(*_: object) -> None:
    raise RuntimeError("assortment_lifecycle_signal_is_append_only")


@event.listens_for(AssortmentLifecycleSignal, "before_delete")
def _reject_signal_delete(*_: object) -> None:
    raise RuntimeError("assortment_lifecycle_signal_is_append_only")
