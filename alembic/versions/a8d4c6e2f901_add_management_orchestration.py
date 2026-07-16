"""add durable management orchestration

Revision ID: a8d4c6e2f901
Revises: de45fa67bc89
Create Date: 2026-07-16 15:10:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "a8d4c6e2f901"
down_revision = "de45fa67bc89"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "orchestration_api_request",
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("response_payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_table(
        "orchestration_job_run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.String(length=128), nullable=False),
        sa.Column("run_key", sa.String(length=255), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(), nullable=False),
        sa.Column("input_sha256", sa.String(length=64), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("worker_id", sa.String(length=128), nullable=False),
        sa.Column("claimed_at", sa.DateTime(), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("result_payload", sa.JSON(), nullable=True),
        sa.Column("result_sha256", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "mode IN ('shadow','production','replay')",
            name="ck_orchestration_job_run_mode",
        ),
        sa.CheckConstraint(
            "status IN ('claimed','running','succeeded','partial','failed','skipped','blocked')",
            name="ck_orchestration_job_run_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "run_key", name="uq_orchestration_job_run_key"),
    )
    op.create_index(
        "ix_orchestration_job_run_scheduled_for",
        "orchestration_job_run",
        ["scheduled_for"],
        unique=False,
    )
    op.create_index(
        "ix_orchestration_job_run_status_lease",
        "orchestration_job_run",
        ["status", "lease_expires_at"],
        unique=False,
    )
    op.create_table(
        "orchestration_delivery_intent",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.String(length=128), nullable=False),
        sa.Column("channel", sa.String(length=64), nullable=False),
        sa.Column("dedupe_key", sa.String(length=255), nullable=False),
        sa.Column("target_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("artifact_refs", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("attempts_count", sa.Integer(), nullable=False),
        sa.Column("claimed_by", sa.String(length=128), nullable=True),
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("external_ref", sa.String(length=255), nullable=True),
        sa.Column("result_sha256", sa.String(length=64), nullable=True),
        sa.Column("last_error_code", sa.String(length=128), nullable=True),
        sa.Column("last_error_detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending','sending','delivered','failed','unknown','manual_review','cancelled')",
            name="ck_orchestration_delivery_intent_status",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["orchestration_job_run.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "job_id",
            "channel",
            "dedupe_key",
            name="uq_orchestration_delivery_intent_dedupe",
        ),
    )
    op.create_index(
        "ix_orchestration_delivery_intent_run_id",
        "orchestration_delivery_intent",
        ["run_id"],
        unique=False,
    )
    op.create_index(
        "ix_orchestration_delivery_intent_status_lease",
        "orchestration_delivery_intent",
        ["status", "lease_expires_at"],
        unique=False,
    )
    op.create_table(
        "orchestration_delivery_attempt",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("intent_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(length=128), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("outcome", sa.String(length=24), nullable=True),
        sa.Column("external_ref", sa.String(length=255), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["intent_id"],
            ["orchestration_delivery_intent.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "intent_id",
            "attempt_number",
            name="uq_orchestration_delivery_attempt_number",
        ),
    )
    op.create_index(
        "ix_orchestration_delivery_attempt_intent_id",
        "orchestration_delivery_attempt",
        ["intent_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_orchestration_delivery_attempt_intent_id",
        table_name="orchestration_delivery_attempt",
    )
    op.drop_table("orchestration_delivery_attempt")
    op.drop_index(
        "ix_orchestration_delivery_intent_status_lease",
        table_name="orchestration_delivery_intent",
    )
    op.drop_index(
        "ix_orchestration_delivery_intent_run_id",
        table_name="orchestration_delivery_intent",
    )
    op.drop_table("orchestration_delivery_intent")
    op.drop_index(
        "ix_orchestration_job_run_status_lease",
        table_name="orchestration_job_run",
    )
    op.drop_index(
        "ix_orchestration_job_run_scheduled_for",
        table_name="orchestration_job_run",
    )
    op.drop_table("orchestration_job_run")
    op.drop_table("orchestration_api_request")
