"""Add receivable workplace v2 cache tables.

Revision ID: 0a1b2c3d4e5f
Revises: 3a4b5c6d7e8f
Create Date: 2026-06-27 09:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0a1b2c3d4e5f"
down_revision = "3a4b5c6d7e8f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "receivable_work_item",
        sa.Column("last_contact_at", sa.DateTime(), nullable=True),
    )

    op.create_table(
        "receivable_open_debt_cache",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("counterparty_ref", sa.String(length=64), nullable=False),
        sa.Column("department_ref", sa.String(length=64), nullable=True),
        sa.Column("source_status", sa.String(length=32), nullable=False),
        sa.Column("documents", sa.JSON(), nullable=False),
        sa.Column("computed_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "snapshot_date",
            "counterparty_ref",
            name="uq_receivable_open_debt_cache_date_counterparty",
        ),
    )
    op.create_index(
        "ix_receivable_open_debt_cache_snapshot_date",
        "receivable_open_debt_cache",
        ["snapshot_date"],
    )
    op.create_index(
        "ix_receivable_open_debt_cache_department_ref",
        "receivable_open_debt_cache",
        ["department_ref"],
    )

    op.create_table(
        "receivable_folder_recommendation_cache",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("status_scope", sa.String(length=32), nullable=False),
        sa.Column("report_revision", sa.String(length=64), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("source_status", sa.String(length=32), nullable=False),
        sa.Column("computed_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "snapshot_date",
            "status_scope",
            name="uq_receivable_folder_recommendation_cache_date_status",
        ),
    )
    op.create_index(
        "ix_receivable_folder_recommendation_cache_snapshot_date",
        "receivable_folder_recommendation_cache",
        ["snapshot_date"],
    )

    op.create_table(
        "receivable_bitrix_user_access",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("bitrix_user_id", sa.String(length=32), nullable=False),
        sa.Column("access_level", sa.String(length=32), nullable=False),
        sa.Column("department_refs", sa.JSON(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("bitrix_user_id", name="uq_receivable_bitrix_user_access_user"),
    )
    op.create_index(
        "ix_receivable_bitrix_user_access_active",
        "receivable_bitrix_user_access",
        ["is_active"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_receivable_bitrix_user_access_active",
        table_name="receivable_bitrix_user_access",
    )
    op.drop_table("receivable_bitrix_user_access")
    op.drop_index(
        "ix_receivable_folder_recommendation_cache_snapshot_date",
        table_name="receivable_folder_recommendation_cache",
    )
    op.drop_table("receivable_folder_recommendation_cache")
    op.drop_index(
        "ix_receivable_open_debt_cache_department_ref",
        table_name="receivable_open_debt_cache",
    )
    op.drop_index(
        "ix_receivable_open_debt_cache_snapshot_date",
        table_name="receivable_open_debt_cache",
    )
    op.drop_table("receivable_open_debt_cache")
    op.drop_column("receivable_work_item", "last_contact_at")
