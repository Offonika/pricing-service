"""add assortment lifecycle v2 shadow fields and immutable history

Revision ID: e5a7c9d1f3b4
Revises: d4f6a8c0e2b3
Create Date: 2026-08-12 22:10:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "e5a7c9d1f3b4"
down_revision = "d4f6a8c0e2b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    table = "assortment_lifecycle_classification"
    columns: tuple[sa.Column, ...] = (
        sa.Column("classification_model", sa.String(32), nullable=False, server_default="v1"),
        sa.Column("legacy_status", sa.String(64), nullable=False, server_default=""),
        sa.Column("target_status", sa.String(64), nullable=False, server_default=""),
        sa.Column("target_status_label", sa.String(128), nullable=False, server_default=""),
        sa.Column("target_reason_codes", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("target_reason_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("demand_state", sa.String(32), nullable=True),
        sa.Column("demand_state_label", sa.String(64), nullable=False, server_default=""),
        sa.Column("demand_reason_codes", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("demand_reason_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("demand_state_since", sa.Date(), nullable=True),
        sa.Column("inventory_cost_per_unit", sa.Numeric(18, 4), nullable=True),
        sa.Column("cost_quartile", sa.String(8), nullable=False, server_default=""),
        sa.Column("comparable_group_key", sa.Text(), nullable=False, server_default=""),
        sa.Column("cost_group_sample_size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("minimum_representation_qty", sa.Integer(), nullable=True),
        sa.Column("first_receipt_at", sa.Date(), nullable=True),
        sa.Column("last_receipt_at", sa.Date(), nullable=True),
        sa.Column("history_age_days", sa.Integer(), nullable=True),
        sa.Column("first_observed_stock_at", sa.Date(), nullable=True),
        sa.Column("observation_from", sa.Date(), nullable=True),
        sa.Column("observation_to", sa.Date(), nullable=True),
        sa.Column("first_sale_at", sa.Date(), nullable=True),
        sa.Column("last_sale_at", sa.Date(), nullable=True),
        sa.Column("sales_qty_short", sa.Numeric(28, 3), nullable=True),
        sa.Column("sales_qty_medium", sa.Numeric(28, 3), nullable=True),
        sa.Column("sales_qty_long", sa.Numeric(28, 3), nullable=True),
        sa.Column("days_in_sale_short", sa.Numeric(28, 3), nullable=True),
        sa.Column("days_in_sale_medium", sa.Numeric(28, 3), nullable=True),
        sa.Column("days_in_sale_long", sa.Numeric(28, 3), nullable=True),
    )
    for column in columns:
        op.add_column(table, column)
    op.create_index("ix_assortment_lifecycle_demand_state", table, ["demand_state"])
    op.create_index("ix_assortment_lifecycle_target_status", table, ["target_status"])

    op.create_table(
        "assortment_lifecycle_classification_history",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("nomenclature_code", sa.String(64), nullable=False),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("target_status", sa.String(64), nullable=False, server_default=""),
        sa.Column("demand_state", sa.String(32), nullable=True),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("classified_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["assortment_lifecycle_classification_run.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id", "nomenclature_code", name="uq_assortment_lifecycle_history_run_sku"
        ),
    )
    op.create_index(
        "ix_assortment_lifecycle_history_sku_time",
        "assortment_lifecycle_classification_history",
        ["nomenclature_code", "classified_at"],
    )
    op.create_index(
        "ix_assortment_lifecycle_history_demand",
        "assortment_lifecycle_classification_history",
        ["demand_state", "classified_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_assortment_lifecycle_history_demand",
        table_name="assortment_lifecycle_classification_history",
    )
    op.drop_index(
        "ix_assortment_lifecycle_history_sku_time",
        table_name="assortment_lifecycle_classification_history",
    )
    op.drop_table("assortment_lifecycle_classification_history")
    op.drop_index(
        "ix_assortment_lifecycle_target_status", table_name="assortment_lifecycle_classification"
    )
    op.drop_index(
        "ix_assortment_lifecycle_demand_state", table_name="assortment_lifecycle_classification"
    )
    for name in (
        "days_in_sale_long",
        "days_in_sale_medium",
        "days_in_sale_short",
        "sales_qty_long",
        "sales_qty_medium",
        "sales_qty_short",
        "last_sale_at",
        "first_sale_at",
        "observation_to",
        "observation_from",
        "first_observed_stock_at",
        "history_age_days",
        "last_receipt_at",
        "first_receipt_at",
        "minimum_representation_qty",
        "cost_group_sample_size",
        "comparable_group_key",
        "cost_quartile",
        "inventory_cost_per_unit",
        "demand_state_since",
        "demand_reason_text",
        "demand_reason_codes",
        "demand_state_label",
        "demand_state",
        "target_reason_text",
        "target_reason_codes",
        "target_status_label",
        "target_status",
        "legacy_status",
        "classification_model",
    ):
        op.drop_column("assortment_lifecycle_classification", name)
