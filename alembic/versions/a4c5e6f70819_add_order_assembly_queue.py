"""Add the CRM-backed customer-order assembly queue snapshot.

Revision ID: a4c5e6f70819
Revises: 9d1f3a5c7e68
Create Date: 2026-08-31
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "a4c5e6f70819"
down_revision = "9d1f3a5c7e68"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "order_assembly_queue_item",
        sa.Column("deal_id", sa.Integer(), nullable=False),
        sa.Column("order_number", sa.String(length=64), nullable=False),
        sa.Column("crm_stage", sa.String(length=64), nullable=False),
        sa.Column("stage_entered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivery_method", sa.String(length=255), nullable=True),
        sa.Column("payment_status", sa.String(length=255), nullable=True),
        sa.Column("assembly_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("urgent", sa.Boolean(), nullable=False),
        sa.Column("urgent_reason", sa.Text(), nullable=True),
        sa.Column("urgent_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence_id", sa.String(length=64), nullable=False),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("deal_id"),
    )
    op.create_index(
        "ix_order_assembly_queue_item_order_number",
        "order_assembly_queue_item",
        ["order_number"],
        unique=False,
    )
    op.create_index(
        "ix_order_assembly_queue_item_priority",
        "order_assembly_queue_item",
        ["urgent", "assembly_due_at", "stage_entered_at"],
        unique=False,
    )

    op.create_table(
        "order_assembly_queue_sync_state",
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=128), nullable=True),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("truncated", sa.Boolean(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source"),
    )


def downgrade() -> None:
    op.drop_table("order_assembly_queue_sync_state")
    op.drop_index(
        "ix_order_assembly_queue_item_priority",
        table_name="order_assembly_queue_item",
    )
    op.drop_index(
        "ix_order_assembly_queue_item_order_number",
        table_name="order_assembly_queue_item",
    )
    op.drop_table("order_assembly_queue_item")
