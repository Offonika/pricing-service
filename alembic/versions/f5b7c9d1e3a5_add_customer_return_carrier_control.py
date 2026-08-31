"""add customer return carrier control

Revision ID: f5b7c9d1e3a5
Revises: e4a6c8d0f2b4
Create Date: 2026-08-31
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "f5b7c9d1e3a5"
down_revision = "e4a6c8d0f2b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "customer_return_shipment",
        sa.Column("carrier", sa.String(length=32), nullable=False),
        sa.Column("tracking_number", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default="registered",
            nullable=False,
        ),
        sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(length=32), server_default="manual", nullable=False),
        sa.Column("source_ref", sa.String(length=128), nullable=True),
        sa.Column("bitrix_case_id", sa.String(length=64), nullable=True),
        sa.Column("site_ticket_id", sa.String(length=64), nullable=True),
        sa.Column("onec_order_ref", sa.String(length=64), nullable=True),
        sa.Column("onec_return_ref", sa.String(length=64), nullable=True),
        sa.Column("created_by_bitrix_user_id", sa.String(length=64), nullable=True),
        sa.Column("picked_up_by_bitrix_user_id", sa.String(length=64), nullable=True),
        sa.Column("carrier_last_status_code", sa.String(length=128), nullable=True),
        sa.Column("carrier_last_status_text", sa.String(length=500), nullable=True),
        sa.Column("carrier_last_event_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("storage_deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("arrived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("picked_up_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("onec_return_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_payload", sa.JSON(), nullable=True),
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
            "status IN ('registered', 'in_transit', 'arrived_at_pickup_point', "
            "'picked_up', 'onec_return_confirmed', 'cancelled', 'exception')",
            name="ck_customer_return_shipment_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "carrier",
            "tracking_number",
            name="uq_customer_return_shipment_carrier_tracking",
        ),
        sa.UniqueConstraint("source_ref", name="uq_customer_return_shipment_source_ref"),
    )
    op.create_index(
        "ix_customer_return_shipment_status_updated",
        "customer_return_shipment",
        ["status", "updated_at"],
    )
    op.create_index(
        "ix_customer_return_shipment_bitrix_case",
        "customer_return_shipment",
        ["bitrix_case_id"],
    )

    op.create_table(
        "customer_return_event",
        sa.Column("shipment_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("normalized_status", sa.String(length=32), nullable=True),
        sa.Column("carrier_status_code", sa.String(length=128), nullable=True),
        sa.Column("carrier_status_text", sa.String(length=500), nullable=True),
        sa.Column("external_event_id", sa.String(length=128), nullable=True),
        sa.Column("dedupe_key", sa.String(length=64), nullable=False),
        sa.Column("actor_bitrix_user_id", sa.String(length=64), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.ForeignKeyConstraint(
            ["shipment_id"], ["customer_return_shipment.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key", name="uq_customer_return_event_dedupe_key"),
    )
    op.create_index(
        "ix_customer_return_event_shipment_occurred",
        "customer_return_event",
        ["shipment_id", "occurred_at", "id"],
    )

    op.create_table(
        "customer_return_action",
        sa.Column("shipment_id", sa.Integer(), nullable=False),
        sa.Column("action_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_token", sa.String(length=64), nullable=True),
        sa.Column("leased_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dedupe_key", sa.String(length=64), nullable=False),
        sa.Column("external_reference", sa.String(length=128), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
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
            "status IN ('pending', 'leased', 'completed', 'skipped', 'failed')",
            name="ck_customer_return_action_status",
        ),
        sa.ForeignKeyConstraint(
            ["shipment_id"], ["customer_return_shipment.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key", name="uq_customer_return_action_dedupe_key"),
    )
    op.create_index(
        "ix_customer_return_action_due",
        "customer_return_action",
        ["status", "next_attempt_at", "due_at", "id"],
    )
    op.create_index(
        "ix_customer_return_action_shipment",
        "customer_return_action",
        ["shipment_id", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_customer_return_action_shipment", table_name="customer_return_action")
    op.drop_index("ix_customer_return_action_due", table_name="customer_return_action")
    op.drop_table("customer_return_action")
    op.drop_index(
        "ix_customer_return_event_shipment_occurred",
        table_name="customer_return_event",
    )
    op.drop_table("customer_return_event")
    op.drop_index(
        "ix_customer_return_shipment_bitrix_case",
        table_name="customer_return_shipment",
    )
    op.drop_index(
        "ix_customer_return_shipment_status_updated",
        table_name="customer_return_shipment",
    )
    op.drop_table("customer_return_shipment")
