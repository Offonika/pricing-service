"""add item_type to competitor_item

Revision ID: 9d3c8c4fd1a1
Revises: 6c6a7b1ef1f0
Create Date: 2025-12-17 00:10:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9d3c8c4fd1a1"
down_revision: str | None = "6c6a7b1ef1f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("competitor_item", sa.Column("item_type", sa.String(length=100), nullable=True))
    op.create_index(
        op.f("ix_competitor_item_item_type"), "competitor_item", ["item_type"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_competitor_item_item_type"), table_name="competitor_item")
    op.drop_column("competitor_item", "item_type")
