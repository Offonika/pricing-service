"""Add versioned order-transfer logistics plans.

Revision ID: b5d6e7f80920
Revises: a4c5e6f70819
Create Date: 2026-09-02
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "b5d6e7f80920"
down_revision = "a4c5e6f70819"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("logistics_transfer", sa.Column("flow_mode", sa.String(32)))
    op.add_column("logistics_transfer", sa.Column("plan_key", sa.String(128)))
    op.add_column("logistics_transfer", sa.Column("plan_version", sa.Integer()))
    op.add_column("logistics_transfer", sa.Column("unit_key", sa.String(128)))
    op.add_column("logistics_transfer", sa.Column("expected_unit_count", sa.Integer()))
    op.add_column(
        "logistics_transfer",
        sa.Column("ready_for_handoff", sa.Boolean(), nullable=False, server_default="0"),
    )
    op.add_column(
        "logistics_transfer",
        sa.Column("is_required", sa.Boolean(), nullable=False, server_default="1"),
    )
    op.create_index(
        "ix_logistics_transfer_order_plan",
        "logistics_transfer",
        ["origin_order_external_id", "plan_version"],
    )

    op.create_table(
        "logistics_order_plan",
        sa.Column("origin_order_external_id", sa.String(64), nullable=False),
        sa.Column("site_order_number", sa.String(32)),
        sa.Column("flow_mode", sa.String(32), nullable=False),
        sa.Column("plan_key", sa.String(128), nullable=False),
        sa.Column("plan_version", sa.Integer(), nullable=False),
        sa.Column(
            "final_warehouse_id",
            sa.Integer(),
            sa.ForeignKey("logistics_warehouse.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sa.String(32), nullable=False, server_default="planned"),
        sa.Column("expected_unit_count", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("synced_at", sa.DateTime(), nullable=False),
        sa.Column("payload", sa.JSON()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.UniqueConstraint(
            "origin_order_external_id",
            "plan_version",
            name="uq_logistics_order_plan_order_version",
        ),
        sa.UniqueConstraint(
            "plan_key",
            "plan_version",
            name="uq_logistics_order_plan_key_version",
        ),
    )
    op.create_index(
        "ix_logistics_order_plan_site_order", "logistics_order_plan", ["site_order_number"]
    )
    op.create_index(
        "ux_logistics_order_plan_active_order",
        "logistics_order_plan",
        ["origin_order_external_id"],
        unique=True,
        postgresql_where=sa.text("is_active"),
        sqlite_where=sa.text("is_active = 1"),
    )

    op.create_table(
        "logistics_order_plan_unit",
        sa.Column(
            "plan_id",
            sa.Integer(),
            sa.ForeignKey("logistics_order_plan.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("unit_key", sa.String(128), nullable=False),
        sa.Column(
            "source_warehouse_id",
            sa.Integer(),
            sa.ForeignKey("logistics_warehouse.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "target_warehouse_id",
            sa.Integer(),
            sa.ForeignKey("logistics_warehouse.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("internal_order_external_id", sa.String(64)),
        sa.Column("transfer_external_id", sa.String(64)),
        sa.Column(
            "transfer_id",
            sa.Integer(),
            sa.ForeignKey("logistics_transfer.id", ondelete="SET NULL"),
        ),
        sa.Column("is_required", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("ready_for_handoff", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("readiness", sa.String(64)),
        sa.Column("synced_at", sa.DateTime(), nullable=False),
        sa.Column("payload", sa.JSON()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.UniqueConstraint("plan_id", "unit_key", name="uq_logistics_order_plan_unit_key"),
        sa.UniqueConstraint("transfer_id", name="uq_logistics_order_plan_unit_transfer"),
    )
    op.create_index(
        "ix_logistics_order_plan_unit_external",
        "logistics_order_plan_unit",
        ["transfer_external_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_logistics_order_plan_unit_external", table_name="logistics_order_plan_unit")
    op.drop_table("logistics_order_plan_unit")
    op.drop_index("ux_logistics_order_plan_active_order", table_name="logistics_order_plan")
    op.drop_index("ix_logistics_order_plan_site_order", table_name="logistics_order_plan")
    op.drop_table("logistics_order_plan")
    op.drop_index("ix_logistics_transfer_order_plan", table_name="logistics_transfer")
    op.drop_column("logistics_transfer", "is_required")
    op.drop_column("logistics_transfer", "ready_for_handoff")
    op.drop_column("logistics_transfer", "expected_unit_count")
    op.drop_column("logistics_transfer", "unit_key")
    op.drop_column("logistics_transfer", "plan_version")
    op.drop_column("logistics_transfer", "plan_key")
    op.drop_column("logistics_transfer", "flow_mode")
