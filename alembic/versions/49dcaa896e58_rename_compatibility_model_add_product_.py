"""rename_compatibility_model_add_product_model

Revision ID: 49dcaa896e58
Revises: 1e16b3d4e6c7
Create Date: 2026-01-11 11:51:39.289422

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "49dcaa896e58"
down_revision: str | None = "1e16b3d4e6c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "competitor_item",
        sa.Column("product_model", sa.String(length=255), nullable=True),
    )
    op.drop_constraint(
        "uq_comp_item_compat_item_brand_model_variant",
        "competitor_item_compatibility",
        type_="unique",
    )
    op.alter_column(
        "competitor_item_compatibility",
        "model",
        new_column_name="compatibility",
        existing_type=sa.String(length=255),
        existing_nullable=False,
    )
    op.create_unique_constraint(
        "uq_comp_item_compat_item_brand_compat_variant",
        "competitor_item_compatibility",
        ["competitor_item_id", "brand", "compatibility", "variant"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_comp_item_compat_item_brand_compat_variant",
        "competitor_item_compatibility",
        type_="unique",
    )
    op.alter_column(
        "competitor_item_compatibility",
        "compatibility",
        new_column_name="model",
        existing_type=sa.String(length=255),
        existing_nullable=False,
    )
    op.create_unique_constraint(
        "uq_comp_item_compat_item_brand_model_variant",
        "competitor_item_compatibility",
        ["competitor_item_id", "brand", "model", "variant"],
    )
    op.drop_column("competitor_item", "product_model")
