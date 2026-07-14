"""add subject to product

Revision ID: 1f2c9c8e3e1b
Revises: ed7e4f7c8f2d
Create Date: 2025-12-01 01:45:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1f2c9c8e3e1b"
down_revision: str | None = "ed7e4f7c8f2d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("product", sa.Column("subject", sa.String(length=100), nullable=True))


def downgrade() -> None:
    op.drop_column("product", "subject")
