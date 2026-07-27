"""add read-only 1C stock availability cache

Revision ID: d1a2b3c4e5f7
Revises: c0f1e2d3a456
Create Date: 2026-07-27 21:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "d1a2b3c4e5f7"
down_revision = "c0f1e2d3a456"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "onec_stock_availability_sync_run",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_key", sa.String(length=160), nullable=False),
        sa.Column("range_start", sa.Date(), nullable=False),
        sa.Column("range_end", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("opening_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("movement_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("day_delta_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("interval_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("summary", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("error_text", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_key", name="uq_onec_stock_availability_sync_run_key"),
    )
    op.create_index(
        "ix_onec_stock_availability_sync_run_range",
        "onec_stock_availability_sync_run",
        ["range_start", "range_end"],
    )
    op.create_index(
        "ix_onec_stock_availability_sync_run_status",
        "onec_stock_availability_sync_run",
        ["status"],
    )

    op.create_table(
        "onec_stock_availability_coverage",
        sa.Column("period_month", sa.Date(), nullable=False),
        sa.Column("covered_from", sa.Date(), nullable=False),
        sa.Column("covered_to", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("last_run_id", sa.BigInteger(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["last_run_id"],
            ["onec_stock_availability_sync_run.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("period_month"),
    )

    op.create_table(
        "onec_stock_day_delta",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("period_month", sa.Date(), nullable=False),
        sa.Column("source_register", sa.String(length=32), nullable=False),
        sa.Column("product_ref", sa.String(length=34), nullable=False),
        sa.Column("product_code", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("warehouse_key", sa.String(length=80), nullable=False),
        sa.Column("warehouse_code", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("opening_qty", sa.Numeric(precision=28, scale=3), nullable=False),
        sa.Column("receipt_qty", sa.Numeric(precision=28, scale=3), nullable=False),
        sa.Column("expense_qty", sa.Numeric(precision=28, scale=3), nullable=False),
        sa.Column("closing_qty", sa.Numeric(precision=28, scale=3), nullable=False),
        sa.Column("available_day", sa.Boolean(), nullable=False),
        sa.Column("last_run_id", sa.BigInteger(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["last_run_id"],
            ["onec_stock_availability_sync_run.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "business_date",
            "source_register",
            "product_ref",
            "warehouse_key",
            name="uq_onec_stock_day_delta_grain",
        ),
    )
    op.create_index(
        "ix_onec_stock_day_delta_product_date",
        "onec_stock_day_delta",
        ["product_ref", "business_date"],
    )
    op.create_index(
        "ix_onec_stock_day_delta_warehouse_date",
        "onec_stock_day_delta",
        ["warehouse_code", "business_date"],
    )

    op.create_table(
        "onec_stock_availability_interval",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("period_month", sa.Date(), nullable=False),
        sa.Column("source_register", sa.String(length=32), nullable=False),
        sa.Column("product_ref", sa.String(length=34), nullable=False),
        sa.Column("product_code", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("warehouse_key", sa.String(length=80), nullable=False),
        sa.Column("warehouse_code", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("available_from", sa.Date(), nullable=False),
        sa.Column("available_to", sa.Date(), nullable=False),
        sa.Column("last_run_id", sa.BigInteger(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["last_run_id"],
            ["onec_stock_availability_sync_run.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_register",
            "product_ref",
            "warehouse_key",
            "available_from",
            "available_to",
            name="uq_onec_stock_availability_interval_grain",
        ),
        sa.CheckConstraint(
            "available_from <= available_to",
            name="ck_onec_stock_availability_interval_dates",
        ),
    )
    op.create_index(
        "ix_onec_stock_availability_interval_product_dates",
        "onec_stock_availability_interval",
        ["product_ref", "available_from", "available_to"],
    )
    op.create_index(
        "ix_onec_stock_availability_interval_warehouse_dates",
        "onec_stock_availability_interval",
        ["warehouse_code", "available_from", "available_to"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_onec_stock_availability_interval_warehouse_dates",
        table_name="onec_stock_availability_interval",
    )
    op.drop_index(
        "ix_onec_stock_availability_interval_product_dates",
        table_name="onec_stock_availability_interval",
    )
    op.drop_table("onec_stock_availability_interval")
    op.drop_index(
        "ix_onec_stock_day_delta_warehouse_date",
        table_name="onec_stock_day_delta",
    )
    op.drop_index(
        "ix_onec_stock_day_delta_product_date",
        table_name="onec_stock_day_delta",
    )
    op.drop_table("onec_stock_day_delta")
    op.drop_table("onec_stock_availability_coverage")
    op.drop_index(
        "ix_onec_stock_availability_sync_run_status",
        table_name="onec_stock_availability_sync_run",
    )
    op.drop_index(
        "ix_onec_stock_availability_sync_run_range",
        table_name="onec_stock_availability_sync_run",
    )
    op.drop_table("onec_stock_availability_sync_run")
