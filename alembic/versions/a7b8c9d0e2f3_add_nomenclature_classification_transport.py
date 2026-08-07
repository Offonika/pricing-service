"""add durable nomenclature classification transport

Revision ID: a7b8c9d0e2f3
Revises: f6a7b8c9d0e2
Create Date: 2026-08-04 18:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "a7b8c9d0e2f3"
down_revision = "f6a7b8c9d0e2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "nomenclature_classification_operation",
        sa.Column("operation_id", sa.String(length=36), nullable=False),
        sa.Column("command_hash", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("approved_by", sa.String(length=150), nullable=False),
        sa.Column("requested_by", sa.String(length=150), nullable=False),
        sa.Column("source", sa.String(length=80), nullable=False),
        sa.Column("target", sa.String(length=80), nullable=False),
        sa.Column("canonical_payload", sa.JSON(), nullable=False),
        sa.Column("dry_run_message_id", sa.String(length=120), nullable=True),
        sa.Column("apply_message_id", sa.String(length=120), nullable=True),
        sa.Column("readback_message_id", sa.String(length=120), nullable=True),
        sa.Column("dry_run_attempts", sa.Integer(), nullable=False),
        sa.Column("apply_attempts", sa.Integer(), nullable=False),
        sa.Column("readback_attempts", sa.Integer(), nullable=False),
        sa.Column("dry_run_sent_at", sa.DateTime(), nullable=True),
        sa.Column("apply_requested_at", sa.DateTime(), nullable=True),
        sa.Column("apply_requested_by", sa.String(length=150), nullable=True),
        sa.Column("apply_sent_at", sa.DateTime(), nullable=True),
        sa.Column("readback_sent_at", sa.DateTime(), nullable=True),
        sa.Column("applied_at", sa.DateTime(), nullable=True),
        sa.Column("last_result_status", sa.String(length=32), nullable=True),
        sa.Column("last_result_at", sa.DateTime(), nullable=True),
        sa.Column("failure_kind", sa.String(length=64), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.CheckConstraint(
            "state IN ("
            "'pending_dry_run','dry_run_sent','dry_run_ok','apply_sent','applying',"
            "'applied','failed','cancelled'"
            ")",
            name="ck_nomenclature_classification_state",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("command_hash", name="uq_nomenclature_classification_command_hash"),
        sa.UniqueConstraint("operation_id", name="uq_nomenclature_classification_operation_id"),
    )
    op.create_index(
        "ix_nomenclature_classification_state_updated",
        "nomenclature_classification_operation",
        ["state", "updated_at"],
    )
    op.create_table(
        "nomenclature_classification_operation_item",
        sa.Column("operation_pk", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("decision_hash", sa.String(length=64), nullable=False),
        sa.Column("nomenclature_code", sa.String(length=64), nullable=False),
        sa.Column("nomenclature_guid", sa.String(length=36), nullable=False),
        sa.Column("active_nomenclature_key", sa.String(length=80), nullable=True),
        sa.Column("canonical_payload", sa.JSON(), nullable=False),
        sa.Column("last_result", sa.String(length=32), nullable=True),
        sa.Column("old_category_guids", sa.JSON(), nullable=True),
        sa.Column("projected_category_guids", sa.JSON(), nullable=True),
        sa.Column("readback_category_guids", sa.JSON(), nullable=True),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.ForeignKeyConstraint(
            ["operation_pk"],
            ["nomenclature_classification_operation.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "active_nomenclature_key",
            name="uq_nomenclature_classification_active_nomenclature",
        ),
        sa.UniqueConstraint("idempotency_key", name="uq_nomenclature_classification_idempotency"),
    )
    op.create_index(
        "ix_nomenclature_classification_item_operation",
        "nomenclature_classification_operation_item",
        ["operation_pk", "nomenclature_code"],
    )
    op.create_table(
        "nomenclature_classification_operation_event",
        sa.Column("operation_pk", sa.Integer(), nullable=False),
        sa.Column("event_key", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=True),
        sa.Column("message_id", sa.String(length=120), nullable=True),
        sa.Column("fingerprint", sa.String(length=64), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.ForeignKeyConstraint(
            ["operation_pk"],
            ["nomenclature_classification_operation.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_key", name="uq_nomenclature_classification_event_key"),
    )
    op.create_index(
        "ix_nomenclature_classification_event_message",
        "nomenclature_classification_operation_event",
        ["message_id"],
    )
    op.create_index(
        "ix_nomenclature_classification_event_operation_created",
        "nomenclature_classification_operation_event",
        ["operation_pk", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_nomenclature_classification_event_operation_created",
        table_name="nomenclature_classification_operation_event",
    )
    op.drop_index(
        "ix_nomenclature_classification_event_message",
        table_name="nomenclature_classification_operation_event",
    )
    op.drop_table("nomenclature_classification_operation_event")
    op.drop_index(
        "ix_nomenclature_classification_item_operation",
        table_name="nomenclature_classification_operation_item",
    )
    op.drop_table("nomenclature_classification_operation_item")
    op.drop_index(
        "ix_nomenclature_classification_state_updated",
        table_name="nomenclature_classification_operation",
    )
    op.drop_table("nomenclature_classification_operation")
