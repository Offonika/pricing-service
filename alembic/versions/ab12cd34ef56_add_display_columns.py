"""add display-specific columns

Revision ID: ab12cd34ef56
Revises: 2e4b1d9c7a6b, 5e0c9e5f5c2d
Create Date: 2026-01-19 22:10:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ab12cd34ef56"
down_revision: str | Sequence[str] | None = ("2e4b1d9c7a6b", "5e0c9e5f5c2d")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "product",
        sa.Column("display_quality", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "product",
        sa.Column("display_construction", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "product",
        sa.Column("display_refresh_rate_hz", sa.Integer(), nullable=True),
    )
    op.add_column(
        "competitor_item",
        sa.Column("attrs_construction", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "competitor_item",
        sa.Column("attrs_refresh_rate_hz", sa.Integer(), nullable=True),
    )

    op.execute(
        "UPDATE product SET display_quality = quality WHERE display_quality IS NULL AND quality IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_column("competitor_item", "attrs_refresh_rate_hz")
    op.drop_column("competitor_item", "attrs_construction")
    op.drop_column("product", "display_refresh_rate_hz")
    op.drop_column("product", "display_construction")
    op.drop_column("product", "display_quality")
