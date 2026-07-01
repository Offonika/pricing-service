"""Add receivable counterparty codes.

Revision ID: 4e5f6a7b8c90
Revises: 0a1b2c3d4e5f
Create Date: 2026-06-27 18:40:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "4e5f6a7b8c90"
down_revision = "0a1b2c3d4e5f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "receivable_balance_snapshot",
        sa.Column("counterparty_code", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "receivable_case",
        sa.Column("counterparty_code", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("receivable_case", "counterparty_code")
    op.drop_column("receivable_balance_snapshot", "counterparty_code")
