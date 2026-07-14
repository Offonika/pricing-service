"""add attrs columns to competitor_item

Revision ID: 5e0c9e5f5c2d
Revises: 49dcaa896e58
Create Date: 2026-01-12 10:00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5e0c9e5f5c2d"
down_revision: str | None = "49dcaa896e58"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "competitor_item",
        sa.Column("attrs_brand", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "competitor_item",
        sa.Column("attrs_model", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "competitor_item",
        sa.Column("attrs_variant", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "competitor_item",
        sa.Column("attrs_color", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "competitor_item",
        sa.Column("attrs_capacity", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "competitor_item",
        sa.Column("attrs_size_inch", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "competitor_item",
        sa.Column("attrs_type", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "competitor_item",
        sa.Column("attrs_quality", sa.String(length=128), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("competitor_item", "attrs_quality")
    op.drop_column("competitor_item", "attrs_type")
    op.drop_column("competitor_item", "attrs_size_inch")
    op.drop_column("competitor_item", "attrs_capacity")
    op.drop_column("competitor_item", "attrs_color")
    op.drop_column("competitor_item", "attrs_variant")
    op.drop_column("competitor_item", "attrs_model")
    op.drop_column("competitor_item", "attrs_brand")
