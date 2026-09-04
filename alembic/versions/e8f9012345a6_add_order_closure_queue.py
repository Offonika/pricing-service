"""Add the audited UT 10.3 customer-order closure queue.

Revision ID: e8f9012345a6
Revises: d7e8f9012345
Create Date: 2026-09-04
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "e8f9012345a6"
down_revision = "d7e8f9012345"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "order_closure_batch",
        sa.Column("public_id", sa.String(36), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("source_type", sa.String(16), nullable=False),
        sa.Column("source_payload", sa.JSON(), nullable=False),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("actor_name", sa.String(255), nullable=True),
        sa.Column("confirmed_by", sa.String(64), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("diagnosis_hash", sa.String(64), nullable=True),
        sa.Column("command_kind", sa.String(16), nullable=True),
        sa.Column("command_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_token", sa.String(64), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_error_code", sa.String(128), nullable=True),
        sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint(
            "status IN ('draft','diagnosed','approved','leased','applied','stale','failed','canceled')",
            name="ck_order_closure_batch_status",
        ),
        sa.CheckConstraint(
            "command_kind IS NULL OR command_kind IN ('diagnose','apply')",
            name="ck_order_closure_batch_command_kind",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id", name="uq_order_closure_batch_public_id"),
    )
    op.create_index(
        "ix_order_closure_batch_status_created", "order_closure_batch", ["status", "created_at"]
    )
    op.create_index(
        "ix_order_closure_batch_lease", "order_closure_batch", ["lease_until", "lease_token"]
    )

    op.create_table(
        "order_closure_item",
        sa.Column("batch_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("input_number", sa.String(64), nullable=False),
        sa.Column("input_period", sa.String(10), nullable=True),
        sa.Column("onec_order_ref", sa.String(36), nullable=True),
        sa.Column("onec_order_number", sa.String(64), nullable=True),
        sa.Column("onec_order_date", sa.Date(), nullable=True),
        sa.Column("site_order_number", sa.String(64), nullable=True),
        sa.Column("department_ref", sa.String(36), nullable=True),
        sa.Column("department_name", sa.String(255), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("eligible", sa.Boolean(), nullable=False),
        sa.Column("blocker_code", sa.String(128), nullable=True),
        sa.Column("blocker_text", sa.Text(), nullable=True),
        sa.Column("facts", sa.JSON(), nullable=False),
        sa.Column("state_hash", sa.String(64), nullable=True),
        sa.Column("reason_code", sa.String(32), nullable=True),
        sa.Column("reason_ref", sa.String(36), nullable=True),
        sa.Column("reason_name", sa.String(255), nullable=True),
        sa.Column("result_document_ref", sa.String(36), nullable=True),
        sa.Column("result_document_number", sa.String(64), nullable=True),
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
        sa.ForeignKeyConstraint(["batch_id"], ["order_closure_batch.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_id", "position", name="uq_order_closure_item_position"),
    )
    op.create_index(
        "ix_order_closure_item_batch_status", "order_closure_item", ["batch_id", "status"]
    )
    op.create_index("ix_order_closure_item_order_ref", "order_closure_item", ["onec_order_ref"])

    op.create_table(
        "order_closure_event",
        sa.Column("batch_id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.ForeignKeyConstraint(["batch_id"], ["order_closure_batch.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["item_id"], ["order_closure_item.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_order_closure_event_batch_created", "order_closure_event", ["batch_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_order_closure_event_batch_created", table_name="order_closure_event")
    op.drop_table("order_closure_event")
    op.drop_index("ix_order_closure_item_order_ref", table_name="order_closure_item")
    op.drop_index("ix_order_closure_item_batch_status", table_name="order_closure_item")
    op.drop_table("order_closure_item")
    op.drop_index("ix_order_closure_batch_lease", table_name="order_closure_batch")
    op.drop_index("ix_order_closure_batch_status_created", table_name="order_closure_batch")
    op.drop_table("order_closure_batch")
