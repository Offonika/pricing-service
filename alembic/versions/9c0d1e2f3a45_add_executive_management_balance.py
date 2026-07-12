"""add executive management balance snapshots

Revision ID: 9c0d1e2f3a45
Revises: 8b9c0d1e2f34
Create Date: 2026-07-12 09:15:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "9c0d1e2f3a45"
down_revision = "8b9c0d1e2f34"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "executive_management_balance_snapshot",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("period_month", sa.Date(), nullable=False),
        sa.Column("balance_date", sa.Date(), nullable=False),
        sa.Column("view_mode", sa.String(length=24), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("source_status", sa.String(length=32), nullable=False),
        sa.Column("freshness_status", sa.String(length=32), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("assets_total", sa.Numeric(18, 2), nullable=False),
        sa.Column("liabilities_total", sa.Numeric(18, 2), nullable=False),
        sa.Column("equity_total", sa.Numeric(18, 2), nullable=False),
        sa.Column("imbalance_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("source_summary", sa.JSON(), nullable=False),
        sa.Column("validation_errors", sa.JSON(), nullable=False),
        sa.Column("generated_at", sa.DateTime(), nullable=False),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
        sa.Column("closed_by", sa.String(length=160), nullable=True),
        sa.Column("close_note", sa.String(length=1000), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "period_month",
            "view_mode",
            "version",
            name="uq_executive_management_balance_period_view_version",
        ),
    )
    op.create_index(
        "ix_executive_management_balance_period_status",
        "executive_management_balance_snapshot",
        ["period_month", "status"],
    )
    op.create_table(
        "executive_management_balance_line",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("snapshot_id", sa.Integer(), nullable=False),
        sa.Column("section", sa.String(length=24), nullable=False),
        sa.Column("line_key", sa.String(length=96), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=True),
        sa.Column("display_order", sa.Integer(), nullable=False),
        sa.Column("source_key", sa.String(length=120), nullable=False),
        sa.Column("source_status", sa.String(length=32), nullable=False),
        sa.Column("source_as_of", sa.Date(), nullable=True),
        sa.Column("note", sa.String(length=1000), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["snapshot_id"], ["executive_management_balance_snapshot.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "snapshot_id", "section", "line_key", name="uq_executive_management_balance_line"
        ),
    )
    op.create_index(
        "ix_executive_management_balance_line_snapshot",
        "executive_management_balance_line",
        ["snapshot_id", "display_order"],
    )
    op.create_table(
        "executive_management_balance_audit",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("snapshot_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=48), nullable=False),
        sa.Column("actor", sa.String(length=160), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["snapshot_id"], ["executive_management_balance_snapshot.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_executive_management_balance_audit_snapshot",
        "executive_management_balance_audit",
        ["snapshot_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_executive_management_balance_audit_snapshot",
        table_name="executive_management_balance_audit",
    )
    op.drop_table("executive_management_balance_audit")
    op.drop_index(
        "ix_executive_management_balance_line_snapshot",
        table_name="executive_management_balance_line",
    )
    op.drop_table("executive_management_balance_line")
    op.drop_index(
        "ix_executive_management_balance_period_status",
        table_name="executive_management_balance_snapshot",
    )
    op.drop_table("executive_management_balance_snapshot")
