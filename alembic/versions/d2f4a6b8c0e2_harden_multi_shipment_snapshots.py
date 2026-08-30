"""harden multi shipment snapshots

Revision ID: d2f4a6b8c0e2
Revises: c1e3a5b7d9f1, c9e1f3a5b7d0
Create Date: 2026-08-29
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "d2f4a6b8c0e2"
down_revision = ("c1e3a5b7d9f1", "c9e1f3a5b7d0")
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "site_order_rtu",
        sa.Column("active", sa.Boolean(), server_default="1", nullable=False),
    )
    op.add_column(
        "site_order_rtu",
        sa.Column("last_seen_snapshot_id", sa.String(length=64), nullable=True),
    )
    op.add_column("site_order_rtu", sa.Column("retired_at", sa.DateTime(), nullable=True))
    op.add_column(
        "site_order_rtu",
        sa.Column("source_revision", sa.String(length=128), nullable=True),
    )
    op.create_index("ix_site_order_rtu_active", "site_order_rtu", ["active", "updated_at"])

    op.add_column(
        "site_order_shipment",
        sa.Column("active", sa.Boolean(), server_default="1", nullable=False),
    )
    op.add_column(
        "site_order_shipment",
        sa.Column("last_seen_snapshot_id", sa.String(length=64), nullable=True),
    )
    op.add_column("site_order_shipment", sa.Column("retired_at", sa.DateTime(), nullable=True))
    op.add_column(
        "site_order_shipment",
        sa.Column("source_revision", sa.String(length=128), nullable=True),
    )
    op.add_column("site_order_shipment", sa.Column("part_number", sa.Integer(), nullable=True))
    op.add_column(
        "site_order_shipment",
        sa.Column("legacy_owned", sa.Boolean(), server_default="0", nullable=False),
    )
    op.create_index(
        "ix_site_order_shipment_active", "site_order_shipment", ["active", "updated_at"]
    )
    op.create_index(
        "uq_site_order_shipment_case_part",
        "site_order_shipment",
        ["case_id", "part_number"],
        unique=True,
    )

    op.add_column(
        "site_order_shipment_notification",
        sa.Column("submitted_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "site_order_shipment_notification",
        sa.Column("delivered_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "site_order_shipment_notification",
        sa.Column("failed_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("site_order_shipment_notification", "failed_at")
    op.drop_column("site_order_shipment_notification", "delivered_at")
    op.drop_column("site_order_shipment_notification", "submitted_at")
    op.drop_index("uq_site_order_shipment_case_part", table_name="site_order_shipment")
    op.drop_index("ix_site_order_shipment_active", table_name="site_order_shipment")
    op.drop_column("site_order_shipment", "legacy_owned")
    op.drop_column("site_order_shipment", "part_number")
    op.drop_column("site_order_shipment", "source_revision")
    op.drop_column("site_order_shipment", "retired_at")
    op.drop_column("site_order_shipment", "last_seen_snapshot_id")
    op.drop_column("site_order_shipment", "active")
    op.drop_index("ix_site_order_rtu_active", table_name="site_order_rtu")
    op.drop_column("site_order_rtu", "source_revision")
    op.drop_column("site_order_rtu", "retired_at")
    op.drop_column("site_order_rtu", "last_seen_snapshot_id")
    op.drop_column("site_order_rtu", "active")
