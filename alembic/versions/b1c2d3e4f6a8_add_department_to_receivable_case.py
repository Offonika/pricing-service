"""add department to receivable case

Revision ID: b1c2d3e4f6a8
Revises: b0c1d2e3f4a5
Create Date: 2026-05-08 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b1c2d3e4f6a8"
down_revision: str | Sequence[str] | None = "b0c1d2e3f4a5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "receivable_balance_snapshot",
        sa.Column("department_ref", sa.String(64), nullable=True),
    )
    op.add_column(
        "receivable_balance_snapshot",
        sa.Column("department_name", sa.String(255), nullable=True),
    )
    op.add_column("receivable_case", sa.Column("department_ref", sa.String(64), nullable=True))
    op.add_column("receivable_case", sa.Column("department_name", sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column("receivable_case", "department_name")
    op.drop_column("receivable_case", "department_ref")
    op.drop_column("receivable_balance_snapshot", "department_name")
    op.drop_column("receivable_balance_snapshot", "department_ref")
