"""add encrypted site service request conversations

Revision ID: e4a6c8d0f2b4
Revises: d2f4a6b8c0e2
Create Date: 2026-08-30
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "e4a6c8d0f2b4"
down_revision = "d2f4a6b8c0e2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "site_service_request_case",
        sa.Column("conversation_snapshot_message_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "site_service_request_case",
        sa.Column("conversation_closed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "site_service_request_case",
        sa.Column("conversation_purge_after", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_site_service_request_case_conversation_purge",
        "site_service_request_case",
        ["conversation_purge_after", "id"],
    )

    op.add_column(
        "site_service_request_command",
        sa.Column("client_request_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "site_service_request_command",
        sa.Column("created_by_bitrix_user_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "site_service_request_command",
        sa.Column("created_by_name", sa.String(length=255), nullable=True),
    )
    op.create_unique_constraint(
        "uq_site_service_request_command_client_request",
        "site_service_request_command",
        ["case_id", "client_request_id"],
    )

    op.create_table(
        "site_service_request_message",
        sa.Column("case_id", sa.Integer(), nullable=False),
        sa.Column("source_message_id", sa.BigInteger(), nullable=True),
        sa.Column("client_request_id", sa.String(length=64), nullable=True),
        sa.Column("message_kind", sa.String(length=24), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("author_kind", sa.String(length=32), nullable=False),
        sa.Column("author_bitrix_user_id", sa.BigInteger(), nullable=True),
        sa.Column("author_name", sa.String(length=255), nullable=True),
        sa.Column("is_visible_to_customer", sa.Boolean(), nullable=False),
        sa.Column("text_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("text_sha256", sa.String(length=64), nullable=False),
        sa.Column("last_snapshot_message_id", sa.BigInteger(), nullable=True),
        sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.CheckConstraint(
            "message_kind IN ('site_message', 'internal_note')",
            name="ck_site_service_request_message_kind",
        ),
        sa.CheckConstraint(
            "direction IN ('inbound', 'outbound', 'internal')",
            name="ck_site_service_request_message_direction",
        ),
        sa.ForeignKeyConstraint(["case_id"], ["site_service_request_case.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "case_id", "source_message_id", name="uq_site_service_request_message_source"
        ),
        sa.UniqueConstraint(
            "case_id", "client_request_id", name="uq_site_service_request_message_client_request"
        ),
    )
    op.create_index(
        "ix_site_service_request_message_case_order",
        "site_service_request_message",
        ["case_id", "created_at", "id"],
    )

    op.create_table(
        "site_service_request_command_file",
        sa.Column("command_id", sa.Integer(), nullable=False),
        sa.Column("client_file_id", sa.String(length=64), nullable=False),
        sa.Column("safe_filename", sa.String(length=255), nullable=False),
        sa.Column("mime_type", sa.String(length=255), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("payload_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("last_error_code", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.CheckConstraint("byte_size >= 0", name="ck_site_service_request_command_file_byte_size"),
        sa.CheckConstraint(
            "status IN ('pending', 'leased', 'applied', 'failed')",
            name="ck_site_service_request_command_file_status",
        ),
        sa.ForeignKeyConstraint(
            ["command_id"], ["site_service_request_command.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "command_id",
            "client_file_id",
            name="uq_site_service_request_command_file_client",
        ),
    )
    op.create_index(
        "ix_site_service_request_command_file_command",
        "site_service_request_command_file",
        ["command_id", "id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_site_service_request_command_file_command",
        table_name="site_service_request_command_file",
    )
    op.drop_table("site_service_request_command_file")
    op.drop_index(
        "ix_site_service_request_message_case_order",
        table_name="site_service_request_message",
    )
    op.drop_table("site_service_request_message")
    op.drop_constraint(
        "uq_site_service_request_command_client_request",
        "site_service_request_command",
        type_="unique",
    )
    op.drop_column("site_service_request_command", "created_by_name")
    op.drop_column("site_service_request_command", "created_by_bitrix_user_id")
    op.drop_column("site_service_request_command", "client_request_id")
    op.drop_index(
        "ix_site_service_request_case_conversation_purge",
        table_name="site_service_request_case",
    )
    op.drop_column("site_service_request_case", "conversation_purge_after")
    op.drop_column("site_service_request_case", "conversation_closed_at")
    op.drop_column("site_service_request_case", "conversation_snapshot_message_id")
