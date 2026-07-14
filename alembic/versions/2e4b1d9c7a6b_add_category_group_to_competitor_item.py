"""add category_group to competitor_item

Revision ID: 2e4b1d9c7a6b
Revises: 6f0f07a5189a
Create Date: 2026-01-11 18:45:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2e4b1d9c7a6b"
down_revision: str | None = "6f0f07a5189a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "competitor_item", sa.Column("category_group", sa.String(length=32), nullable=True)
    )
    op.create_index(
        op.f("ix_competitor_item_category_group"),
        "competitor_item",
        ["category_group"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_competitor_item_category_group"), table_name="competitor_item")
    op.drop_column("competitor_item", "category_group")
