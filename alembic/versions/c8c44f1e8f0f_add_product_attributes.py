"""add category-related product attributes

Revision ID: c8c44f1e8f0f
Revises: 8ae8c8e1c657
Create Date: 2025-12-07 12:30:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c8c44f1e8f0f"
down_revision: str | None = "8ae8c8e1c657"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("product")}

    if "quality" not in cols:
        op.add_column("product", sa.Column("quality", sa.String(length=100), nullable=True))
    if "display_type" not in cols:
        op.add_column("product", sa.Column("display_type", sa.String(length=100), nullable=True))
    if "color" not in cols:
        op.add_column("product", sa.Column("color", sa.String(length=100), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("product")}

    if "color" in cols:
        op.drop_column("product", "color")
    if "display_type" in cols:
        op.drop_column("product", "display_type")
    if "quality" in cols:
        op.drop_column("product", "quality")
