"""add multi shipment fulfillment

Revision ID: c1e3a5b7d9f1
Revises: b8d0f2a4c6e8
Create Date: 2026-08-29
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "c1e3a5b7d9f1"
down_revision = "b8d0f2a4c6e8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "site_order_rtu",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("case_id", sa.Integer(), nullable=False),
        sa.Column("external_id", sa.String(length=64), nullable=False),
        sa.Column("number", sa.String(length=64), nullable=True),
        sa.Column("posted", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("assembled_at", sa.DateTime(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["case_id"], ["site_order_execution_case.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("case_id", "external_id", name="uq_site_order_rtu_case_external"),
    )
    op.create_index(
        "ix_site_order_rtu_case_assembled",
        "site_order_rtu",
        ["case_id", "assembled_at"],
    )

    op.create_table(
        "site_order_rtu_item",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("rtu_id", sa.Integer(), nullable=False),
        sa.Column("product_ref", sa.String(length=64), nullable=False),
        sa.Column("product_code", sa.String(length=64), nullable=True),
        sa.Column("quantity", sa.Numeric(18, 4), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["rtu_id"], ["site_order_rtu.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("rtu_id", "product_ref", name="uq_site_order_rtu_item_product"),
    )
    op.create_index("ix_site_order_rtu_item_product", "site_order_rtu_item", ["product_ref"])

    op.create_table(
        "site_order_shipment",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("case_id", sa.Integer(), nullable=False),
        sa.Column("shipment_key", sa.String(length=128), nullable=False),
        sa.Column("bitrix_shipment_id", sa.Integer(), nullable=True),
        sa.Column("carrier", sa.String(length=64), nullable=True),
        sa.Column("tracking_number", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="planned", nullable=False),
        sa.Column("dispatched_at", sa.DateTime(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(), nullable=True),
        sa.Column("returned_at", sa.DateTime(), nullable=True),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["case_id"], ["site_order_execution_case.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("case_id", "shipment_key", name="uq_site_order_shipment_case_key"),
        sa.UniqueConstraint("bitrix_shipment_id", name="uq_site_order_shipment_bitrix_id"),
    )
    op.create_index(
        "ix_site_order_shipment_case_status",
        "site_order_shipment",
        ["case_id", "status"],
    )
    op.create_index(
        "ix_site_order_shipment_tracking",
        "site_order_shipment",
        ["tracking_number"],
    )

    op.create_table(
        "site_order_shipment_item",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("shipment_id", sa.Integer(), nullable=False),
        sa.Column("bitrix_shipment_item_id", sa.Integer(), nullable=True),
        sa.Column("basket_item_id", sa.Integer(), nullable=True),
        sa.Column("product_ref", sa.String(length=64), nullable=False),
        sa.Column("product_code", sa.String(length=64), nullable=True),
        sa.Column("rtu_external_id", sa.String(length=64), server_default="", nullable=False),
        sa.Column("quantity", sa.Numeric(18, 4), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["shipment_id"], ["site_order_shipment.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "shipment_id",
            "product_ref",
            "rtu_external_id",
            name="uq_site_order_shipment_item_allocation",
        ),
    )
    op.create_index(
        "ix_site_order_shipment_item_product",
        "site_order_shipment_item",
        ["product_ref"],
    )

    op.create_table(
        "site_order_shipment_notification",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("shipment_id", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("shipment_revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("external_ref", sa.String(length=255), nullable=True),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.String(length=1000), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["shipment_id"], ["site_order_shipment.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_site_order_shipment_notification_key"),
        sa.UniqueConstraint(
            "shipment_id",
            "channel",
            "event_type",
            "shipment_revision",
            name="uq_site_order_shipment_notification_revision",
        ),
    )
    op.create_index(
        "ix_site_order_shipment_notification_status",
        "site_order_shipment_notification",
        ["status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_site_order_shipment_notification_status",
        table_name="site_order_shipment_notification",
    )
    op.drop_table("site_order_shipment_notification")
    op.drop_index("ix_site_order_shipment_item_product", table_name="site_order_shipment_item")
    op.drop_table("site_order_shipment_item")
    op.drop_index("ix_site_order_shipment_tracking", table_name="site_order_shipment")
    op.drop_index("ix_site_order_shipment_case_status", table_name="site_order_shipment")
    op.drop_table("site_order_shipment")
    op.drop_index("ix_site_order_rtu_item_product", table_name="site_order_rtu_item")
    op.drop_table("site_order_rtu_item")
    op.drop_index("ix_site_order_rtu_case_assembled", table_name="site_order_rtu")
    op.drop_table("site_order_rtu")
