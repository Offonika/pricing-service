"""add procurement transition queue and audit events

Revision ID: 8b9c0d1e2f34
Revises: 7a8b9c0d1e23
Create Date: 2026-07-10 18:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "8b9c0d1e2f34"
down_revision = "7a8b9c0d1e23"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "procurement_lifecycle_transition_proposal",
        sa.Column("nomenclature_code", sa.String(length=64), nullable=False),
        sa.Column("nomenclature_ref", sa.String(length=64), nullable=True),
        sa.Column("product_guid", sa.String(length=64), nullable=True),
        sa.Column("product_name", sa.String(length=1000), nullable=False),
        sa.Column("folder", sa.String(length=1000), nullable=False),
        sa.Column("action_kind", sa.String(length=32), nullable=False),
        sa.Column("current_status", sa.String(length=64), nullable=False),
        sa.Column("target_status", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("facts", sa.JSON(), nullable=False),
        sa.Column("blockers", sa.JSON(), nullable=False),
        sa.Column("risk_codes", sa.JSON(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("run_key", sa.String(length=160), nullable=False),
        sa.Column("facts_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("responsible_bitrix_user_id", sa.String(length=64), nullable=True),
        sa.Column("responsible_name", sa.String(length=255), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("approved_by_actor", sa.String(length=255), nullable=True),
        sa.Column("approved_by_bitrix_user_id", sa.String(length=64), nullable=True),
        sa.Column("approved_by_name", sa.String(length=255), nullable=True),
        sa.Column("onec_message_id", sa.String(length=160), nullable=True),
        sa.Column("onec_status", sa.String(length=32), nullable=False),
        sa.Column("onec_error", sa.Text(), nullable=True),
        sa.Column("bitrix_readback_value", sa.String(length=128), nullable=True),
        sa.Column("reflected_at", sa.DateTime(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_proc_lifecycle_transition_idempotency"),
    )
    op.create_index(
        "ix_proc_lifecycle_transition_queue",
        "procurement_lifecycle_transition_proposal",
        ["folder", "current_status", "status"],
    )
    op.create_index(
        "ix_proc_lifecycle_transition_run",
        "procurement_lifecycle_transition_proposal",
        ["run_id", "status"],
    )
    op.create_index(
        "ix_proc_lifecycle_transition_product",
        "procurement_lifecycle_transition_proposal",
        ["nomenclature_code", "status"],
    )
    op.create_index(
        "ix_proc_lifecycle_transition_message",
        "procurement_lifecycle_transition_proposal",
        ["onec_message_id"],
    )

    op.create_table(
        "procurement_order_formation_event",
        sa.Column("order_id", sa.Integer(), nullable=True),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("entity_id", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("actor", sa.String(length=255), nullable=False),
        sa.Column("bitrix_user_id", sa.String(length=64), nullable=True),
        sa.Column("user_name", sa.String(length=255), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("before", sa.JSON(), nullable=False),
        sa.Column("after", sa.JSON(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.ForeignKeyConstraint(
            ["order_id"], ["procurement_order_formation.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_proc_order_event_idempotency"),
    )
    op.create_index(
        "ix_proc_order_event_entity",
        "procurement_order_formation_event",
        ["entity_type", "entity_id", "created_at"],
    )
    op.create_index(
        "ix_proc_order_event_order",
        "procurement_order_formation_event",
        ["order_id", "created_at"],
    )
    op.create_index(
        "ix_proc_order_event_type",
        "procurement_order_formation_event",
        ["event_type", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_proc_order_event_type", table_name="procurement_order_formation_event")
    op.drop_index("ix_proc_order_event_order", table_name="procurement_order_formation_event")
    op.drop_index("ix_proc_order_event_entity", table_name="procurement_order_formation_event")
    op.drop_table("procurement_order_formation_event")
    op.drop_index(
        "ix_proc_lifecycle_transition_message",
        table_name="procurement_lifecycle_transition_proposal",
    )
    op.drop_index(
        "ix_proc_lifecycle_transition_product",
        table_name="procurement_lifecycle_transition_proposal",
    )
    op.drop_index(
        "ix_proc_lifecycle_transition_run",
        table_name="procurement_lifecycle_transition_proposal",
    )
    op.drop_index(
        "ix_proc_lifecycle_transition_queue",
        table_name="procurement_lifecycle_transition_proposal",
    )
    op.drop_table("procurement_lifecycle_transition_proposal")
