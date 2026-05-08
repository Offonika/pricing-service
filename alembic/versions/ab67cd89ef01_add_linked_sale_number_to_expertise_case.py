"""add linked sale number to expertise case

Revision ID: ab67cd89ef01
Revises: 9a7b6c5d4e3f
Create Date: 2026-04-12 12:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ab67cd89ef01"
down_revision: str | Sequence[str] | None = "9a7b6c5d4e3f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "expertise_case",
        sa.Column("linked_sale_number", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("expertise_case", "linked_sale_number")
