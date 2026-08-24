from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "2d6f8a0c4b13"
down_revision: str | None = "1b9d3f5a7c21"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "site_order_execution_case",
        sa.Column("pickup_point_warehouse_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "site_order_execution_case",
        sa.Column("storage_started_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "site_order_execution_case",
        sa.Column("storage_deadline_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "site_order_execution_case",
        sa.Column("delivered_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "site_order_execution_case",
        sa.Column("cancelled_at", sa.DateTime(), nullable=True),
    )
    op.create_foreign_key(
        "fk_site_order_execution_case_pickup_point",
        "site_order_execution_case",
        "logistics_warehouse",
        ["pickup_point_warehouse_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "bitrix_chat_action_candidate",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("raw_message_id", sa.Integer(), nullable=True),
        sa.Column("source_chat_id", sa.String(length=64), nullable=False),
        sa.Column("source_message_id", sa.String(length=64), nullable=False),
        sa.Column("source_author_id", sa.String(length=64), nullable=True),
        sa.Column("source_event_at", sa.DateTime(), nullable=True),
        sa.Column("site_order_number", sa.String(length=32), nullable=False),
        sa.Column("bitrix_deal_id", sa.Integer(), nullable=True),
        sa.Column("detected_action", sa.String(length=64), nullable=False),
        sa.Column("pickup_point_warehouse_id", sa.Integer(), nullable=True),
        sa.Column("pickup_point_name", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("nonce", sa.String(length=64), nullable=False),
        sa.Column("active_action", sa.String(length=64), nullable=True),
        sa.Column("active_actor_id", sa.String(length=64), nullable=True),
        sa.Column("action_claimed_at", sa.DateTime(), nullable=True),
        sa.Column("bot_message_id", sa.String(length=64), nullable=True),
        sa.Column("dry_run", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["raw_message_id"],
            ["bitrix_chat_message.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["pickup_point_warehouse_id"],
            ["logistics_warehouse.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_chat_id",
            "source_message_id",
            "site_order_number",
            name="uq_bitrix_chat_action_candidate_source_order",
        ),
        sa.UniqueConstraint("nonce", name="uq_bitrix_chat_action_candidate_nonce"),
    )
    op.create_index(
        "ix_bitrix_chat_action_candidate_status_expires",
        "bitrix_chat_action_candidate",
        ["status", "expires_at"],
    )
    op.create_index(
        "ix_bitrix_chat_action_candidate_order",
        "bitrix_chat_action_candidate",
        ["site_order_number"],
    )

    op.create_table(
        "bitrix_chat_action",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("candidate_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("actor_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("confirmation_step", sa.Integer(), server_default="1", nullable=False),
        sa.Column("before_stage", sa.String(length=64), nullable=True),
        sa.Column("after_stage", sa.String(length=64), nullable=True),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["bitrix_chat_action_candidate.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_bitrix_chat_action_idempotency"),
    )
    op.create_index(
        "ix_bitrix_chat_action_candidate_at",
        "bitrix_chat_action",
        ["candidate_id", "created_at"],
    )
    op.create_index(
        "ix_bitrix_chat_action_actor_at",
        "bitrix_chat_action",
        ["actor_id", "created_at"],
    )

    op.create_table(
        "site_order_fulfillment_outbox",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("candidate_id", sa.Integer(), nullable=True),
        sa.Column("action_id", sa.Integer(), nullable=True),
        sa.Column("depends_on_id", sa.Integer(), nullable=True),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=True),
        sa.Column("target_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="8", nullable=False),
        sa.Column("available_at", sa.DateTime(), nullable=False),
        sa.Column("processed_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.String(length=1000), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["bitrix_chat_action_candidate.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["action_id"],
            ["bitrix_chat_action.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["depends_on_id"],
            ["site_order_fulfillment_outbox.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_site_order_fulfillment_outbox_idempotency",
        ),
    )
    op.create_index(
        "ix_site_order_fulfillment_outbox_status_available",
        "site_order_fulfillment_outbox",
        ["status", "available_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_site_order_fulfillment_outbox_status_available",
        table_name="site_order_fulfillment_outbox",
    )
    op.drop_table("site_order_fulfillment_outbox")
    op.drop_index("ix_bitrix_chat_action_actor_at", table_name="bitrix_chat_action")
    op.drop_index("ix_bitrix_chat_action_candidate_at", table_name="bitrix_chat_action")
    op.drop_table("bitrix_chat_action")
    op.drop_index(
        "ix_bitrix_chat_action_candidate_order",
        table_name="bitrix_chat_action_candidate",
    )
    op.drop_index(
        "ix_bitrix_chat_action_candidate_status_expires",
        table_name="bitrix_chat_action_candidate",
    )
    op.drop_table("bitrix_chat_action_candidate")
    op.drop_constraint(
        "fk_site_order_execution_case_pickup_point",
        "site_order_execution_case",
        type_="foreignkey",
    )
    op.drop_column("site_order_execution_case", "cancelled_at")
    op.drop_column("site_order_execution_case", "delivered_at")
    op.drop_column("site_order_execution_case", "storage_deadline_at")
    op.drop_column("site_order_execution_case", "storage_started_at")
    op.drop_column("site_order_execution_case", "pickup_point_warehouse_id")
