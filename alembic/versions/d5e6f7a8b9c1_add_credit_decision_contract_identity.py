"""add exact contract identity to receivable credit decisions

Revision ID: d5e6f7a8b9c1
Revises: c3d4e5f6a7b9
Create Date: 2026-08-03 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "d5e6f7a8b9c1"
down_revision = "c3d4e5f6a7b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    table = "receivable_credit_decision_operation"
    op.add_column(table, sa.Column("contract_ref", sa.String(length=64), nullable=True))
    op.add_column(table, sa.Column("contract_guid", sa.String(length=36), nullable=True))
    op.add_column(table, sa.Column("contract_code", sa.String(length=32), nullable=True))
    op.add_column(table, sa.Column("contract_name", sa.String(length=255), nullable=True))
    op.add_column(
        table,
        sa.Column("contract_organization_ref", sa.String(length=64), nullable=True),
    )
    op.add_column(
        table,
        sa.Column("contract_organization_guid", sa.String(length=36), nullable=True),
    )
    op.add_column(
        table,
        sa.Column("contract_organization_code", sa.String(length=32), nullable=True),
    )
    op.add_column(
        table,
        sa.Column("contract_organization_name", sa.String(length=255), nullable=True),
    )
    op.add_column(
        table,
        sa.Column("expected_current_debt_control_enabled", sa.Boolean(), nullable=True),
    )
    op.add_column(
        table,
        sa.Column("proposed_debt_control_enabled", sa.Boolean(), nullable=True),
    )
    op.add_column(
        table,
        sa.Column("readback_debt_control_enabled", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    table = "receivable_credit_decision_operation"
    op.drop_column(table, "readback_debt_control_enabled")
    op.drop_column(table, "proposed_debt_control_enabled")
    op.drop_column(table, "expected_current_debt_control_enabled")
    op.drop_column(table, "contract_organization_name")
    op.drop_column(table, "contract_organization_code")
    op.drop_column(table, "contract_organization_guid")
    op.drop_column(table, "contract_organization_ref")
    op.drop_column(table, "contract_name")
    op.drop_column(table, "contract_code")
    op.drop_column(table, "contract_guid")
    op.drop_column(table, "contract_ref")
