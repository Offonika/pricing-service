"""add linked customer order fields to expertise case

Revision ID: 8d41b2e6c7f1
Revises: 7e4a2c1b9d10
Create Date: 2026-04-03 16:45:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8d41b2e6c7f1"
down_revision: str | Sequence[str] | None = "7e4a2c1b9d10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "expertise_case",
        sa.Column("linked_customer_order_ref", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "expertise_case",
        sa.Column("linked_customer_order_number", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("expertise_case", "linked_customer_order_number")
    op.drop_column("expertise_case", "linked_customer_order_ref")
