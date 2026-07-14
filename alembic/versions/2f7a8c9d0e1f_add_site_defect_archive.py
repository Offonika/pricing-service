"""add site defect archive

Revision ID: 2f7a8c9d0e1f
Revises: 0b1c2d3e4f5a
Create Date: 2026-06-11 12:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2f7a8c9d0e1f"
down_revision: str | Sequence[str] | None = "0b1c2d3e4f5a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "site_defect_archive_case",
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column(
            "source",
            sa.String(length=32),
            server_default="old_bitrix_chat",
            nullable=False,
        ),
        sa.Column("source_dialog_id", sa.String(length=64), nullable=False),
        sa.Column("source_post_message_id", sa.String(length=64), nullable=False),
        sa.Column("source_comment_chat_id", sa.String(length=64), nullable=True),
        sa.Column("posted_at", sa.DateTime(), nullable=True),
        sa.Column("author_id", sa.String(length=64), nullable=True),
        sa.Column("author_name", sa.String(length=255), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("summary", sa.String(length=1000), nullable=True),
        sa.Column("problem_type", sa.String(length=64), server_default="other", nullable=False),
        sa.Column("status", sa.String(length=32), server_default="archive", nullable=False),
        sa.Column("search_text", sa.Text(), nullable=False),
        sa.Column("extracted_numbers", sa.JSON(), nullable=True),
        sa.Column("extracted_numbers_text", sa.String(length=1000), nullable=True),
        sa.Column("comment_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("file_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("bitrix_entity_id", sa.String(length=64), nullable=True),
        sa.Column("bitrix_detail_url", sa.String(length=1000), nullable=True),
        sa.Column("bitrix_disk_folder_id", sa.String(length=64), nullable=True),
        sa.Column("bitrix_disk_folder_url", sa.String(length=1000), nullable=True),
        sa.Column("linked_expertise_case_id", sa.Integer(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.ForeignKeyConstraint(
            ["linked_expertise_case_id"],
            ["expertise_case.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_site_defect_archive_case_idempotency_key"),
        sa.UniqueConstraint(
            "source_dialog_id",
            "source_post_message_id",
            name="uq_site_defect_archive_case_source_post",
        ),
    )
    op.create_index(
        "ix_site_defect_archive_case_author_name",
        "site_defect_archive_case",
        ["author_name"],
        unique=False,
    )
    op.create_index(
        "ix_site_defect_archive_case_bitrix_entity_id",
        "site_defect_archive_case",
        ["bitrix_entity_id"],
        unique=False,
    )
    op.create_index(
        "ix_site_defect_archive_case_numbers_text",
        "site_defect_archive_case",
        ["extracted_numbers_text"],
        unique=False,
    )
    op.create_index(
        "ix_site_defect_archive_case_posted_at",
        "site_defect_archive_case",
        ["posted_at"],
        unique=False,
    )
    op.create_index(
        "ix_site_defect_archive_case_problem_type",
        "site_defect_archive_case",
        ["problem_type"],
        unique=False,
    )
    op.create_index(
        "ix_site_defect_archive_case_status",
        "site_defect_archive_case",
        ["status"],
        unique=False,
    )

    op.create_table(
        "site_defect_archive_message",
        sa.Column("case_id", sa.Integer(), nullable=False),
        sa.Column("source_message_id", sa.String(length=64), nullable=False),
        sa.Column("source_chat_id", sa.String(length=64), nullable=True),
        sa.Column("message_kind", sa.String(length=32), nullable=False),
        sa.Column("message_at", sa.DateTime(), nullable=True),
        sa.Column("author_id", sa.String(length=64), nullable=True),
        sa.Column("author_name", sa.String(length=255), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("file_ids", sa.JSON(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.ForeignKeyConstraint(
            ["case_id"],
            ["site_defect_archive_case.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "case_id",
            "source_message_id",
            "message_kind",
            name="uq_site_defect_archive_message_source",
        ),
    )
    op.create_index(
        "ix_site_defect_archive_message_author_name",
        "site_defect_archive_message",
        ["author_name"],
        unique=False,
    )
    op.create_index(
        "ix_site_defect_archive_message_case_id",
        "site_defect_archive_message",
        ["case_id"],
        unique=False,
    )
    op.create_index(
        "ix_site_defect_archive_message_message_at",
        "site_defect_archive_message",
        ["message_at"],
        unique=False,
    )

    op.create_table(
        "site_defect_archive_file",
        sa.Column("case_id", sa.Integer(), nullable=False),
        sa.Column("source_file_id", sa.String(length=64), nullable=True),
        sa.Column("source_message_id", sa.String(length=64), nullable=True),
        sa.Column("name", sa.String(length=500), nullable=False),
        sa.Column("storage_path", sa.String(length=1000), nullable=True),
        sa.Column("content_type", sa.String(length=255), nullable=True),
        sa.Column("extension", sa.String(length=32), nullable=True),
        sa.Column("size", sa.Integer(), nullable=True),
        sa.Column("bitrix_disk_file_id", sa.String(length=64), nullable=True),
        sa.Column("bitrix_disk_url", sa.String(length=1000), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.ForeignKeyConstraint(
            ["case_id"],
            ["site_defect_archive_case.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "case_id",
            "source_file_id",
            "name",
            name="uq_site_defect_archive_file_source",
        ),
    )
    op.create_index(
        "ix_site_defect_archive_file_bitrix_disk_file_id",
        "site_defect_archive_file",
        ["bitrix_disk_file_id"],
        unique=False,
    )
    op.create_index(
        "ix_site_defect_archive_file_case_id",
        "site_defect_archive_file",
        ["case_id"],
        unique=False,
    )
    op.create_index(
        "ix_site_defect_archive_file_extension",
        "site_defect_archive_file",
        ["extension"],
        unique=False,
    )
    op.create_index(
        "ix_site_defect_archive_file_source_file_id",
        "site_defect_archive_file",
        ["source_file_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_site_defect_archive_file_source_file_id", table_name="site_defect_archive_file"
    )
    op.drop_index("ix_site_defect_archive_file_extension", table_name="site_defect_archive_file")
    op.drop_index("ix_site_defect_archive_file_case_id", table_name="site_defect_archive_file")
    op.drop_index(
        "ix_site_defect_archive_file_bitrix_disk_file_id",
        table_name="site_defect_archive_file",
    )
    op.drop_table("site_defect_archive_file")
    op.drop_index(
        "ix_site_defect_archive_message_message_at",
        table_name="site_defect_archive_message",
    )
    op.drop_index(
        "ix_site_defect_archive_message_case_id",
        table_name="site_defect_archive_message",
    )
    op.drop_index(
        "ix_site_defect_archive_message_author_name",
        table_name="site_defect_archive_message",
    )
    op.drop_table("site_defect_archive_message")
    op.drop_index("ix_site_defect_archive_case_status", table_name="site_defect_archive_case")
    op.drop_index(
        "ix_site_defect_archive_case_problem_type",
        table_name="site_defect_archive_case",
    )
    op.drop_index("ix_site_defect_archive_case_posted_at", table_name="site_defect_archive_case")
    op.drop_index(
        "ix_site_defect_archive_case_numbers_text",
        table_name="site_defect_archive_case",
    )
    op.drop_index(
        "ix_site_defect_archive_case_bitrix_entity_id",
        table_name="site_defect_archive_case",
    )
    op.drop_index("ix_site_defect_archive_case_author_name", table_name="site_defect_archive_case")
    op.drop_table("site_defect_archive_case")
