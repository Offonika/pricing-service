"""add_product_compatibility

Revision ID: 7beb0da54240
Revises: 1d2b9d39720a
Create Date: 2025-12-07 13:22:58.756319

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7beb0da54240"
down_revision: str | None = "c8c44f1e8f0f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "productcompatibility",
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("value", sa.String(length=255), nullable=False),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.ForeignKeyConstraint(["product_id"], ["product.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "product_id",
            "value",
            name="uq_product_compatibility_product_value",
        ),
    )
    op.create_index(
        op.f("ix_productcompatibility_product_id"),
        "productcompatibility",
        ["product_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_productcompatibility_product_id"), table_name="productcompatibility")
    op.drop_table("productcompatibility")
