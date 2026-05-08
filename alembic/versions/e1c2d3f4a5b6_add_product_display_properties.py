"""add product display properties

Revision ID: e1c2d3f4a5b6
Revises: d4e5f6a7b8c9
Create Date: 2025-02-06 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e1c2d3f4a5b6"
down_revision: str | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {col["name"] for col in insp.get_columns("product")}

    if "manufacturer" not in cols:
        op.add_column("product", sa.Column("manufacturer", sa.String(length=100), nullable=True))
    if "in_frame" not in cols:
        op.add_column("product", sa.Column("in_frame", sa.String(length=50), nullable=True))
    if "display_diagonal" not in cols:
        op.add_column("product", sa.Column("display_diagonal", sa.String(length=50), nullable=True))
    if "display_resolution" not in cols:
        op.add_column(
            "product", sa.Column("display_resolution", sa.String(length=50), nullable=True)
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {col["name"] for col in insp.get_columns("product")}

    if "display_resolution" in cols:
        op.drop_column("product", "display_resolution")
    if "display_diagonal" in cols:
        op.drop_column("product", "display_diagonal")
    if "in_frame" in cols:
        op.drop_column("product", "in_frame")
    if "manufacturer" in cols:
        op.drop_column("product", "manufacturer")
