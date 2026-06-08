"""add logistics warehouse payload

Revision ID: 0b1c2d3e4f5a
Revises: f0a1b2c3d4e5
Create Date: 2026-05-22 13:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0b1c2d3e4f5a"
down_revision: str | Sequence[str] | None = "f0a1b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("logistics_warehouse", sa.Column("payload", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("logistics_warehouse", "payload")
