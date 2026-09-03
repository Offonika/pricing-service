"""Add durable CRM outbox for confirmed 1C assembly events.

Revision ID: d7e8f9012345
Revises: c6d7e8f90123
Create Date: 2026-09-03
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "d7e8f9012345"
down_revision = "c6d7e8f90123"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "order_assembly_crm_outbox",
        sa.Column("event_key", sa.String(length=160), nullable=False),
        sa.Column("crm_status", sa.String(length=32), nullable=False),
        sa.Column("event_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("assembly_source", sa.String(length=32), nullable=False),
        sa.Column("assembly_ref", sa.String(length=64), nullable=False),
        sa.Column("onec_order_number", sa.String(length=64), nullable=True),
        sa.Column("site_order_number", sa.String(length=64), nullable=False),
        sa.Column("execution_status", sa.String(length=2), nullable=False),
        sa.Column("delivery_code", sa.String(length=32), nullable=False),
        sa.Column("payment_mode", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("crm_response", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_key", name="uq_order_assembly_crm_outbox_event_key"),
    )
    op.create_index(
        "ix_order_assembly_crm_outbox_status_next",
        "order_assembly_crm_outbox",
        ["status", "next_attempt_at"],
        unique=False,
    )
    op.create_index(
        "ix_order_assembly_crm_outbox_order",
        "order_assembly_crm_outbox",
        ["site_order_number", "event_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_order_assembly_crm_outbox_order", table_name="order_assembly_crm_outbox")
    op.drop_index(
        "ix_order_assembly_crm_outbox_status_next", table_name="order_assembly_crm_outbox"
    )
    op.drop_table("order_assembly_crm_outbox")
