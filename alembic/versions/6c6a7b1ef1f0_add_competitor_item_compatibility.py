"""add competitor item compatibility table

Revision ID: 6c6a7b1ef1f0
Revises: 1fbb2e7c4c3a
Create Date: 2025-12-16 23:00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6c6a7b1ef1f0"
down_revision: str | None = "1fbb2e7c4c3a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "competitor_item_compatibility",
        sa.Column("competitor_item_id", sa.Integer(), nullable=False),
        sa.Column("brand", sa.String(length=128), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("variant", sa.String(length=50), nullable=True),
        sa.Column("source", sa.String(length=50), nullable=True),
        sa.Column("notes", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.ForeignKeyConstraint(["competitor_item_id"], ["competitor_item.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "competitor_item_id",
            "brand",
            "model",
            "variant",
            name="uq_comp_item_compat_item_brand_model_variant",
        ),
    )
    op.create_index(
        op.f("ix_competitor_item_compatibility_competitor_item_id"),
        "competitor_item_compatibility",
        ["competitor_item_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_competitor_item_compatibility_brand"),
        "competitor_item_compatibility",
        ["brand"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_competitor_item_compatibility_brand"), table_name="competitor_item_compatibility"
    )
    op.drop_index(
        op.f("ix_competitor_item_compatibility_competitor_item_id"),
        table_name="competitor_item_compatibility",
    )
    op.drop_table("competitor_item_compatibility")
