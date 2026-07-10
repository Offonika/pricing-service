"""add executive dashboard

Revision ID: 3d4e5f6a7b80
Revises: 2c3d4e5f6a70
Create Date: 2026-06-27 17:30:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "3d4e5f6a7b80"
down_revision = "2c3d4e5f6a70"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "executive_dashboard_snapshot",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("revision", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("source_freshness", sa.JSON(), nullable=False),
        sa.Column("computed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "snapshot_date",
            "revision",
            name="uq_executive_dashboard_snapshot_date_revision",
        ),
    )
    op.create_index(
        "ix_executive_dashboard_snapshot_date_status",
        "executive_dashboard_snapshot",
        ["snapshot_date", "status"],
    )
    op.create_table(
        "executive_action_item",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("stable_key", sa.String(length=160), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("domain", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.String(length=1000), nullable=True),
        sa.Column("amount", sa.Numeric(18, 2), nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("responsible_bitrix_user_id", sa.String(length=64), nullable=True),
        sa.Column("deadline_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("source_system", sa.String(length=64), nullable=False),
        sa.Column("source_ref", sa.String(length=255), nullable=True),
        sa.Column("dedupe_key", sa.String(length=255), nullable=False),
        sa.Column("drilldown_url", sa.String(length=1000), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("stable_key", name="uq_executive_action_item_stable_key"),
        sa.UniqueConstraint("dedupe_key", name="uq_executive_action_item_dedupe_key"),
    )
    op.create_index(
        "ix_executive_action_item_business_date_status",
        "executive_action_item",
        ["business_date", "status"],
    )
    op.create_index(
        "ix_executive_action_item_domain_severity",
        "executive_action_item",
        ["domain", "severity"],
    )
    op.create_index(
        "ix_executive_action_item_responsible",
        "executive_action_item",
        ["responsible_bitrix_user_id", "status"],
    )
    op.create_table(
        "executive_source_freshness",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source_key", sa.String(length=120), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("source_status", sa.String(length=32), nullable=False),
        sa.Column("source_as_of", sa.DateTime(), nullable=True),
        sa.Column("max_lag_days", sa.Integer(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_key",
            "business_date",
            name="uq_executive_source_freshness_key_date",
        ),
    )
    op.create_index(
        "ix_executive_source_freshness_status",
        "executive_source_freshness",
        ["source_status"],
    )


def downgrade() -> None:
    op.drop_index("ix_executive_source_freshness_status", table_name="executive_source_freshness")
    op.drop_table("executive_source_freshness")
    op.drop_index("ix_executive_action_item_responsible", table_name="executive_action_item")
    op.drop_index("ix_executive_action_item_domain_severity", table_name="executive_action_item")
    op.drop_index(
        "ix_executive_action_item_business_date_status",
        table_name="executive_action_item",
    )
    op.drop_table("executive_action_item")
    op.drop_index(
        "ix_executive_dashboard_snapshot_date_status",
        table_name="executive_dashboard_snapshot",
    )
    op.drop_table("executive_dashboard_snapshot")
