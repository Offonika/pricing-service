"""Add Bitrix logistics identity, fallback tokens, and stage outbox.

Revision ID: 9d1f3a5c7e68
Revises: a4c6e8f0b2d3
Create Date: 2026-08-26
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "9d1f3a5c7e68"
down_revision = "a4c6e8f0b2d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "logistics_user",
        sa.Column("bitrix_user_id", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ux_logistics_user_bitrix_user_id",
        "logistics_user",
        ["bitrix_user_id"],
        unique=True,
    )

    op.create_table(
        "logistics_web_launch_token",
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["logistics_user.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_logistics_web_launch_token_hash"),
    )
    op.create_index(
        "ix_logistics_web_launch_token_expires",
        "logistics_web_launch_token",
        ["expires_at"],
        unique=False,
    )

    op.create_table(
        "site_order_stage_outbox",
        sa.Column("case_id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("site_order_number", sa.String(length=32), nullable=False),
        sa.Column("bitrix_deal_id", sa.Integer(), nullable=True),
        sa.Column("source_event_type", sa.String(length=64), nullable=False),
        sa.Column("target_stage", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("last_live_stage", sa.String(length=64), nullable=True),
        sa.Column("last_error", sa.String(length=1000), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("applied_at", sa.DateTime(), nullable=True),
        sa.Column("timeline_written_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.ForeignKeyConstraint(
            ["case_id"],
            ["site_order_execution_case.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["site_order_execution_event.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", name="uq_site_order_stage_outbox_event"),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_site_order_stage_outbox_idempotency",
        ),
    )
    op.create_index(
        "ix_site_order_stage_outbox_status_next",
        "site_order_stage_outbox",
        ["status", "next_attempt_at"],
        unique=False,
    )
    op.create_index(
        "ix_site_order_stage_outbox_case_id",
        "site_order_stage_outbox",
        ["case_id", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_site_order_stage_outbox_case_id", table_name="site_order_stage_outbox")
    op.drop_index("ix_site_order_stage_outbox_status_next", table_name="site_order_stage_outbox")
    op.drop_table("site_order_stage_outbox")
    op.drop_index("ix_logistics_web_launch_token_expires", table_name="logistics_web_launch_token")
    op.drop_table("logistics_web_launch_token")
    op.drop_index("ux_logistics_user_bitrix_user_id", table_name="logistics_user")
    op.drop_column("logistics_user", "bitrix_user_id")
