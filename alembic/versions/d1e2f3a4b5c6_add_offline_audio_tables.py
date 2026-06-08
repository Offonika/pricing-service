"""add offline store audio tables

Revision ID: d1e2f3a4b5c6
Revises: c0d1e2f3a4b6
Create Date: 2026-05-14 18:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d1e2f3a4b5c6"
down_revision: str | None = "c0d1e2f3a4b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if insp.has_table("offline_dialog"):
        return

    op.create_table(
        "offline_dialog",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("dialog_id", sa.String(length=255), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False, server_default="offline_store"),
        sa.Column("store_id", sa.String(length=128), nullable=False, server_default="unknown"),
        sa.Column("store_name", sa.String(length=255), nullable=True),
        sa.Column("pc_id", sa.String(length=128), nullable=True),
        sa.Column("device_id", sa.String(length=128), nullable=True),
        sa.Column("recorder_model", sa.String(length=128), nullable=True),
        sa.Column("recorder_serial", sa.String(length=128), nullable=True),
        sa.Column("record_id", sa.String(length=128), nullable=True),
        sa.Column("microphone_model", sa.String(length=128), nullable=True),
        sa.Column("upload_protocol", sa.String(length=32), nullable=True),
        sa.Column("manifest_schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("ingest_pipeline_version", sa.String(length=64), nullable=False),
        sa.Column("hardware_profile_version", sa.String(length=128), nullable=False),
        sa.Column("asr_profile_version", sa.String(length=128), nullable=False),
        sa.Column("storage_layout_version", sa.String(length=64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_sec", sa.Float(), nullable=True),
        sa.Column("audio_storage_path", sa.String(length=1024), nullable=True),
        sa.Column("manifest_storage_path", sa.String(length=1024), nullable=True),
        sa.Column("original_landing_path", sa.String(length=1024), nullable=True),
        sa.Column("original_filename", sa.String(length=512), nullable=True),
        sa.Column("audio_sha256", sa.String(length=128), nullable=True),
        sa.Column("format", sa.String(length=128), nullable=True),
        sa.Column("codec", sa.String(length=64), nullable=True),
        sa.Column("sample_rate_hz", sa.Integer(), nullable=True),
        sa.Column("channel_count", sa.Integer(), nullable=True),
        sa.Column("file_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("normalized_manifest_json", sa.JSON(), nullable=True),
        sa.Column("quality_flags", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("ingest_status", sa.String(length=32), nullable=False, server_default="received"),
        sa.Column("asr_status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column(
            "analysis_status", sa.String(length=32), nullable=False, server_default="pending"
        ),
        sa.Column("ingest_error", sa.Text(), nullable=True),
        sa.Column("asr_error", sa.Text(), nullable=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("stored_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("asr_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("asr_completed_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dialog_id", name="uq_offline_dialog_dialog_id"),
    )
    op.create_index("ix_offline_dialog_store_started", "offline_dialog", ["store_id", "started_at"])
    op.create_index("ix_offline_dialog_ingest_status", "offline_dialog", ["ingest_status"])
    op.create_index("ix_offline_dialog_asr_status", "offline_dialog", ["asr_status"])
    op.create_index("ix_offline_dialog_audio_sha256", "offline_dialog", ["audio_sha256"])
    op.create_index("ix_offline_dialog_device_id", "offline_dialog", ["device_id"])

    op.create_table(
        "offline_dialog_transcript",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("dialog_id", sa.String(length=255), nullable=False),
        sa.Column("language", sa.String(length=16), nullable=False, server_default="ru"),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("asr_profile_version", sa.String(length=128), nullable=False),
        sa.Column("transcript_text", sa.Text(), nullable=False),
        sa.Column("segments_json", sa.JSON(), nullable=True),
        sa.Column("channel_roles_json", sa.JSON(), nullable=True),
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
        sa.ForeignKeyConstraint(["dialog_id"], ["offline_dialog.dialog_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dialog_id", name="uq_offline_dialog_transcript_dialog_id"),
    )
    op.create_index(
        "ix_offline_dialog_transcript_dialog_id", "offline_dialog_transcript", ["dialog_id"]
    )

    op.create_table(
        "offline_dialog_analysis",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("dialog_id", sa.String(length=255), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("sentiment", sa.String(length=32), nullable=True),
        sa.Column("outcome", sa.String(length=64), nullable=True),
        sa.Column("business_flags_json", sa.JSON(), nullable=True),
        sa.Column("quality_flags_json", sa.JSON(), nullable=True),
        sa.Column("analysis_model", sa.String(length=255), nullable=True),
        sa.Column("analysis_profile_version", sa.String(length=128), nullable=True),
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
        sa.ForeignKeyConstraint(["dialog_id"], ["offline_dialog.dialog_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dialog_id", name="uq_offline_dialog_analysis_dialog_id"),
    )
    op.create_index(
        "ix_offline_dialog_analysis_dialog_id", "offline_dialog_analysis", ["dialog_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_offline_dialog_analysis_dialog_id", table_name="offline_dialog_analysis")
    op.drop_table("offline_dialog_analysis")
    op.drop_index("ix_offline_dialog_transcript_dialog_id", table_name="offline_dialog_transcript")
    op.drop_table("offline_dialog_transcript")
    op.drop_index("ix_offline_dialog_device_id", table_name="offline_dialog")
    op.drop_index("ix_offline_dialog_audio_sha256", table_name="offline_dialog")
    op.drop_index("ix_offline_dialog_asr_status", table_name="offline_dialog")
    op.drop_index("ix_offline_dialog_ingest_status", table_name="offline_dialog")
    op.drop_index("ix_offline_dialog_store_started", table_name="offline_dialog")
    op.drop_table("offline_dialog")
