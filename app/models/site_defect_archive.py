from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.models.base import Base


class SiteDefectArchiveCase(Base):
    __tablename__ = "site_defect_archive_case"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_site_defect_archive_case_idempotency_key"),
        UniqueConstraint(
            "source_dialog_id",
            "source_post_message_id",
            name="uq_site_defect_archive_case_source_post",
        ),
        Index("ix_site_defect_archive_case_posted_at", "posted_at"),
        Index("ix_site_defect_archive_case_author_name", "author_name"),
        Index("ix_site_defect_archive_case_problem_type", "problem_type"),
        Index("ix_site_defect_archive_case_status", "status"),
        Index("ix_site_defect_archive_case_numbers_text", "extracted_numbers_text"),
        Index("ix_site_defect_archive_case_bitrix_entity_id", "bitrix_entity_id"),
    )

    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[str] = mapped_column(
        String(32), nullable=False, default="old_bitrix_chat", server_default="old_bitrix_chat"
    )
    source_dialog_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_post_message_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_comment_chat_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    author_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    author_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    summary: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    problem_type: Mapped[str] = mapped_column(
        String(64), nullable=False, default="other", server_default="other"
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="archive", server_default="archive"
    )
    search_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    extracted_numbers: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    extracted_numbers_text: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    comment_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    file_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    bitrix_entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bitrix_detail_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    bitrix_disk_folder_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bitrix_disk_folder_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    linked_expertise_case_id: Mapped[int | None] = mapped_column(
        ForeignKey("expertise_case.id", ondelete="SET NULL"),
        nullable=True,
    )
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    messages = relationship(
        "SiteDefectArchiveMessage",
        back_populates="case",
        cascade="all, delete-orphan",
        order_by="SiteDefectArchiveMessage.message_at.asc()",
    )
    files = relationship(
        "SiteDefectArchiveFile",
        back_populates="case",
        cascade="all, delete-orphan",
        order_by="SiteDefectArchiveFile.name.asc()",
    )
    linked_expertise = relationship("ExpertiseCase")


class SiteDefectArchiveMessage(Base):
    __tablename__ = "site_defect_archive_message"
    __table_args__ = (
        UniqueConstraint(
            "case_id",
            "source_message_id",
            "message_kind",
            name="uq_site_defect_archive_message_source",
        ),
        Index("ix_site_defect_archive_message_case_id", "case_id"),
        Index("ix_site_defect_archive_message_message_at", "message_at"),
        Index("ix_site_defect_archive_message_author_name", "author_name"),
    )

    case_id: Mapped[int] = mapped_column(
        ForeignKey("site_defect_archive_case.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_message_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_chat_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    message_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    author_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    author_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    file_ids: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    case = relationship("SiteDefectArchiveCase", back_populates="messages")


class SiteDefectArchiveFile(Base):
    __tablename__ = "site_defect_archive_file"
    __table_args__ = (
        UniqueConstraint(
            "case_id",
            "source_file_id",
            "name",
            name="uq_site_defect_archive_file_source",
        ),
        Index("ix_site_defect_archive_file_case_id", "case_id"),
        Index("ix_site_defect_archive_file_source_file_id", "source_file_id"),
        Index("ix_site_defect_archive_file_extension", "extension"),
        Index("ix_site_defect_archive_file_bitrix_disk_file_id", "bitrix_disk_file_id"),
    )

    case_id: Mapped[int] = mapped_column(
        ForeignKey("site_defect_archive_case.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_file_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    storage_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    extension: Mapped[str | None] = mapped_column(String(32), nullable=True)
    size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bitrix_disk_file_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bitrix_disk_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    case = relationship("SiteDefectArchiveCase", back_populates="files")
