"""add encrypted SMS observability journal

Revision ID: c3e5a7b9d1f2
Revises: b2d4f6a8c0e1
Create Date: 2026-08-10 14:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "c3e5a7b9d1f2"
down_revision = "b2d4f6a8c0e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sms_journal_api_request",
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("response_payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_table(
        "sms_journal_attempt",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("create_idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("source_system", sa.String(length=64), nullable=False),
        sa.Column("source_entity_type", sa.String(length=64), nullable=False),
        sa.Column("source_entity_id", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=True),
        sa.Column("recipient_phone_encrypted", sa.Text(), nullable=False),
        sa.Column("recipient_phone_hash", sa.String(length=64), nullable=False),
        sa.Column("recipient_phone_masked", sa.String(length=32), nullable=False),
        sa.Column("message_text_encrypted", sa.Text(), nullable=False),
        sa.Column("message_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("contains_redacted_secret", sa.Boolean(), nullable=False),
        sa.Column("character_count", sa.Integer(), nullable=False),
        sa.Column("encoding", sa.String(length=8), nullable=False),
        sa.Column("estimated_segments", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("sender_name", sa.String(length=64), nullable=True),
        sa.Column("provider_message_id", sa.String(length=255), nullable=True),
        sa.Column("send_status", sa.String(length=16), nullable=False),
        sa.Column("delivery_status", sa.String(length=16), nullable=False),
        sa.Column("provider_error_code", sa.String(length=128), nullable=True),
        sa.Column("provider_error_detail_encrypted", sa.Text(), nullable=True),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(), nullable=True),
        sa.Column("billed_segments", sa.Integer(), nullable=True),
        sa.Column("unit_price", sa.Numeric(precision=12, scale=4), nullable=True),
        sa.Column("total_cost", sa.Numeric(precision=14, scale=4), nullable=True),
        sa.Column("reconciliation_period", sa.String(length=7), nullable=True),
        sa.Column("retention_expires_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "delivery_status IN ('pending','delivered','undelivered','expired','unknown')",
            name="ck_sms_journal_attempt_delivery_status",
        ),
        sa.CheckConstraint(
            "encoding IN ('GSM-7','UCS-2')",
            name="ck_sms_journal_attempt_encoding",
        ),
        sa.CheckConstraint(
            "send_status IN ('pending','accepted','failed','unknown')",
            name="ck_sms_journal_attempt_send_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("create_idempotency_key"),
    )
    op.create_index(
        "ix_sms_journal_attempt_created_at",
        "sms_journal_attempt",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        "ix_sms_journal_attempt_phone_hash",
        "sms_journal_attempt",
        ["recipient_phone_hash"],
        unique=False,
    )
    op.create_index(
        "ix_sms_journal_attempt_provider_message",
        "sms_journal_attempt",
        ["provider", "provider_message_id"],
        unique=False,
    )
    op.create_index(
        "ix_sms_journal_attempt_source",
        "sms_journal_attempt",
        ["source_system", "source_entity_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_sms_journal_attempt_source", table_name="sms_journal_attempt")
    op.drop_index("ix_sms_journal_attempt_provider_message", table_name="sms_journal_attempt")
    op.drop_index("ix_sms_journal_attempt_phone_hash", table_name="sms_journal_attempt")
    op.drop_index("ix_sms_journal_attempt_created_at", table_name="sms_journal_attempt")
    op.drop_table("sms_journal_attempt")
    op.drop_table("sms_journal_api_request")
