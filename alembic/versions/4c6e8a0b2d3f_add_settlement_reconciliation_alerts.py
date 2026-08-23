"""add customer settlement reconciliation and alert state

Revision ID: 4c6e8a0b2d3f
Revises: 2a4c6e8f0b1d
Create Date: 2026-08-22 23:10:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "4c6e8a0b2d3f"
down_revision = "2a4c6e8f0b1d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "customer_settlement_reconciliation_run",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("report_date", sa.Date(), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("report_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("expected_count", sa.Integer(), nullable=False),
        sa.Column("matched_count", sa.Integer(), nullable=False),
        sa.Column("mismatch_count", sa.Integer(), nullable=False),
        sa.Column("max_abs_difference", sa.Numeric(18, 2), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('matched','mismatched','blocked')",
            name="ck_customer_settlement_reconciliation_status",
        ),
        sa.CheckConstraint(
            "expected_count >= 0 AND matched_count >= 0 AND mismatch_count >= 0",
            name="ck_customer_settlement_reconciliation_counts",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("report_date", "report_hash", name="uq_customer_settlement_report_run"),
    )
    op.create_table(
        "customer_settlement_alert_state",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("channel", sa.String(length=64), nullable=False),
        sa.Column("current_level", sa.String(length=16), nullable=False),
        sa.Column("last_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "current_level IN ('ok','warning','critical')",
            name="ck_customer_settlement_alert_state_level",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("channel", name="uq_customer_settlement_alert_state_channel"),
    )
    op.create_table(
        "customer_settlement_alert_outbox",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_key", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("external_ref", sa.String(length=128), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('pending','sent','failed')",
            name="ck_customer_settlement_alert_outbox_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_key", name="uq_customer_settlement_alert_outbox_event"),
    )
    op.create_index(
        "ix_customer_settlement_alert_outbox_pending",
        "customer_settlement_alert_outbox",
        ["status", "next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_customer_settlement_alert_outbox_pending",
        table_name="customer_settlement_alert_outbox",
    )
    op.drop_table("customer_settlement_alert_outbox")
    op.drop_table("customer_settlement_alert_state")
    op.drop_table("customer_settlement_reconciliation_run")
