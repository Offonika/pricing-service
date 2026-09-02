from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class LogisticsOrderPlan(Base):
    __tablename__ = "logistics_order_plan"
    __table_args__ = (
        UniqueConstraint(
            "origin_order_external_id",
            "plan_version",
            name="uq_logistics_order_plan_order_version",
        ),
        UniqueConstraint(
            "plan_key",
            "plan_version",
            name="uq_logistics_order_plan_key_version",
        ),
        Index("ix_logistics_order_plan_site_order", "site_order_number"),
        Index(
            "ux_logistics_order_plan_active_order",
            "origin_order_external_id",
            unique=True,
            postgresql_where=text("is_active"),
            sqlite_where=text("is_active = 1"),
        ),
    )

    origin_order_external_id: Mapped[str] = mapped_column(String(64), nullable=False)
    site_order_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    flow_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    plan_key: Mapped[str] = mapped_column(String(128), nullable=False)
    plan_version: Mapped[int] = mapped_column(Integer, nullable=False)
    final_warehouse_id: Mapped[int] = mapped_column(
        ForeignKey("logistics_warehouse.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="planned", server_default="planned"
    )
    expected_unit_count: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    synced_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    final_warehouse = relationship("LogisticsWarehouse")
    units = relationship(
        "LogisticsOrderPlanUnit",
        back_populates="plan",
        cascade="all, delete-orphan",
    )


class LogisticsOrderPlanUnit(Base):
    __tablename__ = "logistics_order_plan_unit"
    __table_args__ = (
        UniqueConstraint("plan_id", "unit_key", name="uq_logistics_order_plan_unit_key"),
        UniqueConstraint("transfer_id", name="uq_logistics_order_plan_unit_transfer"),
        Index("ix_logistics_order_plan_unit_external", "transfer_external_id"),
    )

    plan_id: Mapped[int] = mapped_column(
        ForeignKey("logistics_order_plan.id", ondelete="CASCADE"), nullable=False
    )
    unit_key: Mapped[str] = mapped_column(String(128), nullable=False)
    source_warehouse_id: Mapped[int] = mapped_column(
        ForeignKey("logistics_warehouse.id", ondelete="RESTRICT"), nullable=False
    )
    target_warehouse_id: Mapped[int] = mapped_column(
        ForeignKey("logistics_warehouse.id", ondelete="RESTRICT"), nullable=False
    )
    internal_order_external_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    transfer_external_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    transfer_id: Mapped[int | None] = mapped_column(
        ForeignKey("logistics_transfer.id", ondelete="SET NULL"), nullable=True
    )
    is_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    ready_for_handoff: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    readiness: Mapped[str | None] = mapped_column(String(64), nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    plan = relationship("LogisticsOrderPlan", back_populates="units")
    source_warehouse = relationship("LogisticsWarehouse", foreign_keys=[source_warehouse_id])
    target_warehouse = relationship("LogisticsWarehouse", foreign_keys=[target_warehouse_id])
    transfer = relationship("LogisticsTransfer")
