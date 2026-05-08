"""add product subject column

Revision ID: c9d1e2f3a4b5
Revises: b9c0d1e2f3a4
Create Date: 2025-02-06 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c9d1e2f3a4b5"
down_revision: str | None = "b9c0d1e2f3a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("product", sa.Column("subject", sa.String(length=100), nullable=True))


def downgrade() -> None:
    op.drop_column("product", "subject")
