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


class LogisticsWarehouse(Base):
    __tablename__ = "logistics_warehouse"

    external_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )


class LogisticsDriver(Base):
    __tablename__ = "logistics_driver"

    external_id: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )


class LogisticsUser(Base):
    __tablename__ = "logistics_user"

    external_id: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    telegram_user_id: Mapped[int | None] = mapped_column(nullable=True, unique=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    default_warehouse_id: Mapped[int | None] = mapped_column(
        ForeignKey("logistics_warehouse.id", ondelete="SET NULL"),
        nullable=True,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    default_warehouse = relationship("LogisticsWarehouse")


class LogisticsTransfer(Base):
    __tablename__ = "logistics_transfer"
    __table_args__ = (Index("ix_logistics_transfer_barcode", "barcode"),)

    external_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    document_number: Mapped[str] = mapped_column(String(64), nullable=False)
    document_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    source_warehouse_id: Mapped[int] = mapped_column(
        ForeignKey("logistics_warehouse.id", ondelete="RESTRICT"),
        nullable=False,
    )
    target_warehouse_id: Mapped[int] = mapped_column(
        ForeignKey("logistics_warehouse.id", ondelete="RESTRICT"),
        nullable=False,
    )
    final_recipient_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    barcode: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    onec_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    onec_deleted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    source_warehouse = relationship("LogisticsWarehouse", foreign_keys=[source_warehouse_id])
    target_warehouse = relationship("LogisticsWarehouse", foreign_keys=[target_warehouse_id])
    state = relationship("LogisticsTransferState", back_populates="transfer", uselist=False)


class LogisticsTransferState(Base):
    __tablename__ = "logistics_transfer_state"
    __table_args__ = (
        Index("ix_logistics_transfer_state_status_dropoff", "status", "dropoff_warehouse_id"),
        Index("ix_logistics_transfer_state_status_current", "status", "current_warehouse_id"),
    )
    id = None

    transfer_id: Mapped[int] = mapped_column(
        ForeignKey("logistics_transfer.id", ondelete="CASCADE"),
        primary_key=True,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    current_warehouse_id: Mapped[int | None] = mapped_column(
        ForeignKey("logistics_warehouse.id", ondelete="SET NULL"),
        nullable=True,
    )
    dropoff_warehouse_id: Mapped[int | None] = mapped_column(
        ForeignKey("logistics_warehouse.id", ondelete="SET NULL"),
        nullable=True,
    )
    driver_id: Mapped[int | None] = mapped_column(
        ForeignKey("logistics_driver.id", ondelete="SET NULL"),
        nullable=True,
    )
    last_event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    last_event_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("logistics_user.id", ondelete="SET NULL"),
        nullable=True,
    )
    last_document_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    transfer = relationship("LogisticsTransfer", back_populates="state")
    current_warehouse = relationship("LogisticsWarehouse", foreign_keys=[current_warehouse_id])
    dropoff_warehouse = relationship("LogisticsWarehouse", foreign_keys=[dropoff_warehouse_id])
    driver = relationship("LogisticsDriver", foreign_keys=[driver_id])
    last_user = relationship("LogisticsUser", foreign_keys=[last_user_id])


class LogisticsTransferEvent(Base):
    __tablename__ = "logistics_transfer_event"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_logistics_transfer_event_idempotency_key"),
        Index("ix_logistics_transfer_event_transfer_event_at", "transfer_id", "event_at"),
    )

    transfer_id: Mapped[int] = mapped_column(
        ForeignKey("logistics_transfer.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    event_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    warehouse_id: Mapped[int | None] = mapped_column(
        ForeignKey("logistics_warehouse.id", ondelete="SET NULL"),
        nullable=True,
    )
    dropoff_warehouse_id: Mapped[int | None] = mapped_column(
        ForeignKey("logistics_warehouse.id", ondelete="SET NULL"),
        nullable=True,
    )
    driver_id: Mapped[int | None] = mapped_column(
        ForeignKey("logistics_driver.id", ondelete="SET NULL"),
        nullable=True,
    )
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("logistics_user.id", ondelete="SET NULL"),
        nullable=True,
    )
    comment: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    document_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    transfer = relationship("LogisticsTransfer")
    warehouse = relationship("LogisticsWarehouse", foreign_keys=[warehouse_id])
    dropoff_warehouse = relationship("LogisticsWarehouse", foreign_keys=[dropoff_warehouse_id])
    driver = relationship("LogisticsDriver", foreign_keys=[driver_id])
    user = relationship("LogisticsUser", foreign_keys=[user_id])
    photos = relationship(
        "LogisticsEventPhoto",
        back_populates="event",
        cascade="all, delete-orphan",
    )


class LogisticsEventPhoto(Base):
    __tablename__ = "logistics_event_photo"

    event_id: Mapped[int] = mapped_column(
        ForeignKey("logistics_transfer_event.id", ondelete="CASCADE"),
        nullable=False,
    )
    telegram_file_id: Mapped[str] = mapped_column(String(255), nullable=False)
    file_kind: Mapped[str] = mapped_column(
        String(32), nullable=False, default="photo", server_default="photo"
    )
    comment: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    event = relationship("LogisticsTransferEvent", back_populates="photos")


class LogisticsDraft(Base):
    __tablename__ = "logistics_draft"
    __table_args__ = (
        Index(
            "ix_logistics_draft_actor_open_unique",
            "actor_user_id",
            unique=True,
            postgresql_where=text("status = 'open'"),
            sqlite_where=text("status = 'open'"),
        ),
    )

    draft_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="open", server_default="open"
    )
    warehouse_id: Mapped[int] = mapped_column(
        ForeignKey("logistics_warehouse.id", ondelete="RESTRICT"),
        nullable=False,
    )
    actor_user_id: Mapped[int] = mapped_column(
        ForeignKey("logistics_user.id", ondelete="RESTRICT"),
        nullable=False,
    )
    driver_id: Mapped[int | None] = mapped_column(
        ForeignKey("logistics_driver.id", ondelete="SET NULL"),
        nullable=True,
    )
    default_dropoff_warehouse_id: Mapped[int | None] = mapped_column(
        ForeignKey("logistics_warehouse.id", ondelete="SET NULL"),
        nullable=True,
    )
    comment: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    warehouse = relationship("LogisticsWarehouse", foreign_keys=[warehouse_id])
    actor_user = relationship("LogisticsUser", foreign_keys=[actor_user_id])
    driver = relationship("LogisticsDriver", foreign_keys=[driver_id])
    default_dropoff_warehouse = relationship(
        "LogisticsWarehouse",
        foreign_keys=[default_dropoff_warehouse_id],
    )
    items = relationship(
        "LogisticsDraftItem",
        back_populates="draft",
        cascade="all, delete-orphan",
    )


class LogisticsDraftItem(Base):
    __tablename__ = "logistics_draft_item"
    __table_args__ = (
        UniqueConstraint("draft_id", "transfer_id", name="uq_logistics_draft_item_transfer"),
    )

    draft_id: Mapped[int] = mapped_column(
        ForeignKey("logistics_draft.id", ondelete="CASCADE"),
        nullable=False,
    )
    transfer_id: Mapped[int] = mapped_column(
        ForeignKey("logistics_transfer.id", ondelete="CASCADE"),
        nullable=False,
    )
    barcode: Mapped[str] = mapped_column(String(255), nullable=False)
    dropoff_warehouse_id: Mapped[int | None] = mapped_column(
        ForeignKey("logistics_warehouse.id", ondelete="SET NULL"),
        nullable=True,
    )
    scan_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("logistics_user.id", ondelete="SET NULL"),
        nullable=True,
    )
    scan_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    draft = relationship("LogisticsDraft", back_populates="items")
    transfer = relationship("LogisticsTransfer")
    dropoff_warehouse = relationship("LogisticsWarehouse", foreign_keys=[dropoff_warehouse_id])
    scan_user = relationship("LogisticsUser", foreign_keys=[scan_user_id])


class LogisticsBotSession(Base):
    __tablename__ = "logistics_bot_session"
    __table_args__ = (UniqueConstraint("chat_id", name="uq_logistics_bot_session_chat_id"),)

    chat_id: Mapped[int] = mapped_column(nullable=False)
    telegram_user_id: Mapped[int] = mapped_column(nullable=False)
    actor_user_id: Mapped[int] = mapped_column(
        ForeignKey("logistics_user.id", ondelete="CASCADE"),
        nullable=False,
    )
    draft_id: Mapped[int] = mapped_column(
        ForeignKey("logistics_draft.id", ondelete="CASCADE"),
        nullable=False,
    )
    draft_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status_message_id: Mapped[int | None] = mapped_column(nullable=True)
    scan_error_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    recent_errors: Mapped[list[dict] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    actor_user = relationship("LogisticsUser")
    draft = relationship("LogisticsDraft")
    photos = relationship(
        "LogisticsBotSessionPhoto",
        back_populates="session",
        cascade="all, delete-orphan",
    )


class LogisticsBotSessionPhoto(Base):
    __tablename__ = "logistics_bot_session_photo"

    session_id: Mapped[int] = mapped_column(
        ForeignKey("logistics_bot_session.id", ondelete="CASCADE"),
        nullable=False,
    )
    telegram_file_id: Mapped[str] = mapped_column(String(255), nullable=False)
    comment: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    session = relationship("LogisticsBotSession", back_populates="photos")
