"""add site service request intake and delivery state

Revision ID: 2c4d6e8f0a12
Revises: 1b9d3f5a7c21
Create Date: 2026-08-22 13:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "2c4d6e8f0a12"
down_revision: str | None = "1b9d3f5a7c21"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "site_service_request_case",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source_ticket_id", sa.BigInteger(), nullable=False),
        sa.Column("bitrix_item_id", sa.BigInteger(), nullable=True),
        sa.Column("crm_contact_id", sa.BigInteger(), nullable=True),
        sa.Column("crm_company_id", sa.BigInteger(), nullable=True),
        sa.Column("crm_deal_id", sa.BigInteger(), nullable=True),
        sa.Column("assigned_user_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "assignment_state",
            sa.String(length=32),
            server_default="waiting",
            nullable=False,
        ),
        sa.Column("round_robin_seq", sa.Integer(), server_default="0", nullable=False),
        sa.Column("intake_mode", sa.String(length=32), nullable=True),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("first_response_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sla_paused_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_response_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("escalated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("latest_inbound_message_id", sa.BigInteger(), nullable=True),
        sa.Column("latest_outbound_message_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "sync_status",
            sa.String(length=32),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("last_error_code", sa.String(length=128), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "intake_mode IS NULL OR " "intake_mode IN ('during_open_shift', 'outside_open_shift')",
            name="ck_site_service_request_case_intake_mode",
        ),
        sa.CheckConstraint(
            "round_robin_seq >= 0",
            name="ck_site_service_request_case_round_robin_seq",
        ),
        sa.CheckConstraint("version > 0", name="ck_site_service_request_case_version"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "bitrix_item_id",
            name="uq_site_service_request_case_bitrix_item",
        ),
        sa.UniqueConstraint(
            "source_ticket_id",
            name="uq_site_service_request_case_source_ticket",
        ),
    )
    op.create_index(
        "ix_site_service_request_case_assignment",
        "site_service_request_case",
        ["assignment_state", "assigned_user_id"],
    )
    op.create_index(
        "ix_site_service_request_case_first_response_due",
        "site_service_request_case",
        ["first_response_due_at"],
    )
    op.create_index(
        "ix_site_service_request_case_sync_status",
        "site_service_request_case",
        ["sync_status", "updated_at"],
    )

    op.create_table(
        "site_service_request_event",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.String(length=255), nullable=False),
        sa.Column("case_id", sa.Integer(), nullable=False),
        sa.Column("source_message_id", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("payload_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=128), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "attempts >= 0",
            name="ck_site_service_request_event_attempts",
        ),
        sa.ForeignKeyConstraint(
            ["case_id"],
            ["site_service_request_case.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", name="uq_site_service_request_event_id"),
    )
    op.create_index(
        "ix_site_service_request_event_case_message",
        "site_service_request_event",
        ["case_id", "source_message_id"],
    )
    op.create_index(
        "ix_site_service_request_event_processing",
        "site_service_request_event",
        ["status", "next_retry_at"],
    )

    op.create_table(
        "site_service_request_file",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("case_id", sa.Integer(), nullable=False),
        sa.Column("source_message_id", sa.BigInteger(), nullable=False),
        sa.Column("source_file_id", sa.BigInteger(), nullable=False),
        sa.Column("safe_filename", sa.String(length=255), nullable=False),
        sa.Column("mime_type", sa.String(length=255), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("bitrix_file_id", sa.String(length=64), nullable=True),
        sa.Column("bitrix_object_id", sa.String(length=64), nullable=True),
        sa.Column("temporary_path", sa.String(length=1024), nullable=True),
        sa.Column("last_error_code", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "byte_size >= 0",
            name="ck_site_service_request_file_byte_size",
        ),
        sa.ForeignKeyConstraint(
            ["case_id"],
            ["site_service_request_case.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_message_id",
            "source_file_id",
            name="uq_site_service_request_file_source",
        ),
    )
    op.create_index(
        "ix_site_service_request_file_case_status",
        "site_service_request_file",
        ["case_id", "status"],
    )

    op.create_table(
        "site_service_request_command",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("case_id", sa.Integer(), nullable=False),
        sa.Column("command_key", sa.String(length=255), nullable=False),
        sa.Column("reply_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column("reply_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_message_id", sa.BigInteger(), nullable=True),
        sa.Column("ack_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "attempts >= 0",
            name="ck_site_service_request_command_attempts",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'leased', 'applied', 'failed')",
            name="ck_site_service_request_command_status",
        ),
        sa.ForeignKeyConstraint(
            ["case_id"],
            ["site_service_request_case.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "command_key",
            name="uq_site_service_request_command_key",
        ),
    )
    op.create_index(
        "ix_site_service_request_command_lease",
        "site_service_request_command",
        ["status", "lease_until"],
    )

    op.create_table(
        "site_service_request_nonce",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("nonce", sa.String(length=36), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("nonce", name="uq_site_service_request_nonce_nonce"),
    )
    op.create_index(
        "ix_site_service_request_nonce_expires",
        "site_service_request_nonce",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_site_service_request_nonce_expires",
        table_name="site_service_request_nonce",
    )
    op.drop_table("site_service_request_nonce")

    op.drop_index(
        "ix_site_service_request_command_lease",
        table_name="site_service_request_command",
    )
    op.drop_table("site_service_request_command")

    op.drop_index(
        "ix_site_service_request_file_case_status",
        table_name="site_service_request_file",
    )
    op.drop_table("site_service_request_file")

    op.drop_index(
        "ix_site_service_request_event_processing",
        table_name="site_service_request_event",
    )
    op.drop_index(
        "ix_site_service_request_event_case_message",
        table_name="site_service_request_event",
    )
    op.drop_table("site_service_request_event")

    op.drop_index(
        "ix_site_service_request_case_sync_status",
        table_name="site_service_request_case",
    )
    op.drop_index(
        "ix_site_service_request_case_first_response_due",
        table_name="site_service_request_case",
    )
    op.drop_index(
        "ix_site_service_request_case_assignment",
        table_name="site_service_request_case",
    )
    op.drop_table("site_service_request_case")
