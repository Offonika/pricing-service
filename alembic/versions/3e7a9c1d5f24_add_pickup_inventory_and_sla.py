from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "3e7a9c1d5f24"
down_revision: str | None = "2d6f8a0c4b13"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("site_order_execution_case") as batch_op:
        batch_op.add_column(sa.Column("notification_confirmed_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("sla_started_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("hold_until", sa.Date(), nullable=True))
    with op.batch_alter_table("site_order_execution_event") as batch_op:
        batch_op.add_column(sa.Column("warehouse_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("actor_ref", sa.String(length=64), nullable=True))
        batch_op.create_foreign_key(
            "fk_site_order_execution_event_warehouse",
            "logistics_warehouse",
            ["warehouse_id"],
            ["id"],
            ondelete="SET NULL",
        )

    op.create_table(
        "bitrix_chat_reaction",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.String(length=64), nullable=False),
        sa.Column("reaction", sa.String(length=32), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("removed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["bitrix_chat_message.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "message_id",
            "actor_id",
            "reaction",
            name="uq_bitrix_chat_reaction_identity",
        ),
    )
    op.create_index(
        "ix_bitrix_chat_reaction_actor_active",
        "bitrix_chat_reaction",
        ["actor_id", "is_active"],
    )

    op.create_table(
        "pickup_inventory_run",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("dialog_id", sa.String(length=64), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "dialog_id",
            "business_date",
            name="uq_pickup_inventory_run_dialog_date",
        ),
    )
    op.create_index(
        "ix_pickup_inventory_run_status_date",
        "pickup_inventory_run",
        ["status", "business_date"],
    )

    op.create_table(
        "pickup_inventory_submission",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("warehouse_id", sa.Integer(), nullable=True),
        sa.Column("source_message_id", sa.Integer(), nullable=False),
        sa.Column("supersedes_submission_id", sa.Integer(), nullable=True),
        sa.Column("author_id", sa.String(length=64), nullable=True),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("parser_version", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["pickup_inventory_run.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["warehouse_id"],
            ["logistics_warehouse.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_message_id"],
            ["bitrix_chat_message.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_submission_id"],
            ["pickup_inventory_submission.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_message_id",
            "warehouse_id",
            "revision",
            name="uq_pickup_inventory_submission_message_warehouse",
        ),
        sa.UniqueConstraint(
            "run_id",
            "warehouse_id",
            "revision",
            name="uq_pickup_inventory_submission_revision",
        ),
    )
    op.create_index(
        "ix_pickup_inventory_submission_warehouse_at",
        "pickup_inventory_submission",
        ["warehouse_id", "submitted_at"],
    )
    op.create_index(
        "ix_pickup_inventory_submission_status",
        "pickup_inventory_submission",
        ["status"],
    )

    op.create_table(
        "pickup_inventory_item",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("submission_id", sa.Integer(), nullable=False),
        sa.Column("site_order_number", sa.String(length=32), nullable=False),
        sa.Column(
            "validation_status", sa.String(length=32), server_default="valid", nullable=False
        ),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["submission_id"],
            ["pickup_inventory_submission.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "submission_id",
            "site_order_number",
            name="uq_pickup_inventory_item_submission_order",
        ),
    )
    op.create_index(
        "ix_pickup_inventory_item_order",
        "pickup_inventory_item",
        ["site_order_number"],
    )


def downgrade() -> None:
    op.drop_index("ix_pickup_inventory_item_order", table_name="pickup_inventory_item")
    op.drop_table("pickup_inventory_item")
    op.drop_index(
        "ix_pickup_inventory_submission_status",
        table_name="pickup_inventory_submission",
    )
    op.drop_index(
        "ix_pickup_inventory_submission_warehouse_at",
        table_name="pickup_inventory_submission",
    )
    op.drop_table("pickup_inventory_submission")
    op.drop_index("ix_pickup_inventory_run_status_date", table_name="pickup_inventory_run")
    op.drop_table("pickup_inventory_run")
    op.drop_index(
        "ix_bitrix_chat_reaction_actor_active",
        table_name="bitrix_chat_reaction",
    )
    op.drop_table("bitrix_chat_reaction")
    with op.batch_alter_table("site_order_execution_event") as batch_op:
        batch_op.drop_constraint(
            "fk_site_order_execution_event_warehouse",
            type_="foreignkey",
        )
        batch_op.drop_column("actor_ref")
        batch_op.drop_column("warehouse_id")
    with op.batch_alter_table("site_order_execution_case") as batch_op:
        batch_op.drop_column("hold_until")
        batch_op.drop_column("sla_started_at")
        batch_op.drop_column("notification_confirmed_at")
