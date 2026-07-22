"""add customer price type expert quality samples

Revision ID: c0f1e2d3a456
Revises: b9e5d7f3a012
Create Date: 2026-07-20 09:20:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "c0f1e2d3a456"
down_revision: str | None = "b9e5d7f3a012"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "customer_price_type_quality_sample",
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("snapshot_id", sa.Integer(), nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("system_group", sa.String(length=64), nullable=False),
        sa.Column("correct_group", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("selected_by", sa.String(length=255), nullable=False),
        sa.Column("selected_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("reviewed_by", sa.String(length=255), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.CheckConstraint(
            "system_group IN ('manager_work','isolate','recovery','data_check',"
            "'special_review','downgrade_approval','no_action')",
            name="ck_customer_price_type_quality_sample_system_group",
        ),
        sa.CheckConstraint(
            "correct_group IS NULL OR correct_group IN "
            "('manager_work','isolate','recovery','data_check','special_review',"
            "'downgrade_approval','no_action')",
            name="ck_customer_price_type_quality_sample_correct_group",
        ),
        sa.CheckConstraint(
            "status IN ('pending','reviewed')",
            name="ck_customer_price_type_quality_sample_status",
        ),
        sa.CheckConstraint("version > 0", name="ck_customer_price_type_quality_sample_version"),
        sa.ForeignKeyConstraint(
            ["profile_id"], ["customer_price_type_profile.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["run_id"], ["customer_price_type_run.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["snapshot_id"], ["customer_price_type_snapshot.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("snapshot_id", name="uq_customer_price_type_quality_sample_snapshot"),
    )
    op.create_index(
        "ix_customer_price_type_quality_sample_run_status",
        "customer_price_type_quality_sample",
        ["run_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_customer_price_type_quality_sample_run_group",
        "customer_price_type_quality_sample",
        ["run_id", "system_group"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_customer_price_type_quality_sample_run_group",
        table_name="customer_price_type_quality_sample",
    )
    op.drop_index(
        "ix_customer_price_type_quality_sample_run_status",
        table_name="customer_price_type_quality_sample",
    )
    op.drop_table("customer_price_type_quality_sample")
