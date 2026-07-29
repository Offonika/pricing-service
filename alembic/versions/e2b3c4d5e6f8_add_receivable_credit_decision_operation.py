"""add durable receivable credit-decision operations

Revision ID: e2b3c4d5e6f8
Revises: d1a2b3c4e5f7
Create Date: 2026-07-28 23:20:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "e2b3c4d5e6f8"
down_revision = "d1a2b3c4e5f7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "receivable_credit_decision_operation",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("bitrix_entity_type_id", sa.Integer(), nullable=False),
        sa.Column("bitrix_item_id", sa.String(length=64), nullable=False),
        sa.Column("bitrix_category_id", sa.Integer(), nullable=True),
        sa.Column("bitrix_stage_id", sa.String(length=96), nullable=False),
        sa.Column("bitrix_revision", sa.String(length=96), nullable=False),
        sa.Column("moved_by_user_id", sa.String(length=32), nullable=False),
        sa.Column("decision_id", sa.String(length=128), nullable=False),
        sa.Column("decision_hash", sa.String(length=64), nullable=False),
        sa.Column("counterparty_key", sa.String(length=96), nullable=False),
        sa.Column("active_counterparty_key", sa.String(length=96), nullable=True),
        sa.Column("counterparty_ref", sa.String(length=64), nullable=False),
        sa.Column("counterparty_guid", sa.String(length=36), nullable=False),
        sa.Column("counterparty_code", sa.String(length=32), nullable=False),
        sa.Column("counterparty_name", sa.String(length=255), nullable=False),
        sa.Column("expected_current_limit", sa.Numeric(18, 2), nullable=False),
        sa.Column("expected_current_depth", sa.Integer(), nullable=False),
        sa.Column("proposed_limit", sa.Numeric(18, 2), nullable=False),
        sa.Column("proposed_depth", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("approved_by", sa.String(length=32), nullable=False),
        sa.Column("approved_at", sa.DateTime(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("dry_run_message_id", sa.String(length=160), nullable=True),
        sa.Column("apply_message_id", sa.String(length=160), nullable=True),
        sa.Column("readback_message_id", sa.String(length=160), nullable=True),
        sa.Column("dry_run_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("apply_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("readback_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_result_status", sa.String(length=32), nullable=True),
        sa.Column("last_result_at", sa.DateTime(), nullable=True),
        sa.Column("dry_run_sent_at", sa.DateTime(), nullable=True),
        sa.Column("apply_sent_at", sa.DateTime(), nullable=True),
        sa.Column("readback_sent_at", sa.DateTime(), nullable=True),
        sa.Column("applied_at", sa.DateTime(), nullable=True),
        sa.Column("readback_limit", sa.Numeric(18, 2), nullable=True),
        sa.Column("readback_depth", sa.Integer(), nullable=True),
        sa.Column(
            "bitrix_sync_pending",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("source_payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "state IN ("
            "'pending_dry_run','dry_run_sent','dry_run_ok','apply_sent','applying',"
            "'applied','failed','cancelled'"
            ")",
            name="ck_receivable_credit_decision_state",
        ),
        sa.CheckConstraint(
            "expected_current_limit >= 0 AND proposed_limit >= 0",
            name="ck_receivable_credit_decision_nonnegative_limits",
        ),
        sa.CheckConstraint(
            "expected_current_depth >= 0 AND proposed_depth >= 0",
            name="ck_receivable_credit_decision_nonnegative_depths",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "bitrix_entity_type_id",
            "bitrix_item_id",
            "bitrix_revision",
            name="uq_receivable_credit_decision_item_revision",
        ),
        sa.UniqueConstraint(
            "active_counterparty_key",
            name="uq_receivable_credit_decision_active_counterparty",
        ),
    )
    op.create_index(
        "ix_receivable_credit_decision_state_updated",
        "receivable_credit_decision_operation",
        ["state", "updated_at"],
    )
    op.create_index(
        "ix_receivable_credit_decision_counterparty",
        "receivable_credit_decision_operation",
        ["counterparty_key", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_receivable_credit_decision_counterparty",
        table_name="receivable_credit_decision_operation",
    )
    op.drop_index(
        "ix_receivable_credit_decision_state_updated",
        table_name="receivable_credit_decision_operation",
    )
    op.drop_table("receivable_credit_decision_operation")
