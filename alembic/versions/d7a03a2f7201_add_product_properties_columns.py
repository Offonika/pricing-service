"""add product property columns

Revision ID: d7a03a2f7201
Revises: 0f3e3c21b4e5
Create Date: 2025-12-01 12:00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d7a03a2f7201"
down_revision: str | None = "0f3e3c21b4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    cols = {c["name"] for c in insp.get_columns("product")}

    if "quality" not in cols:
        op.add_column("product", sa.Column("quality", sa.String(length=100), nullable=True))
    if "in_frame" not in cols:
        op.add_column("product", sa.Column("in_frame", sa.String(length=50), nullable=True))
    if "display_type" not in cols:
        op.add_column("product", sa.Column("display_type", sa.String(length=100), nullable=True))


def downgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    cols = {c["name"] for c in insp.get_columns("product")}

    if "display_type" in cols:
        op.drop_column("product", "display_type")
    if "in_frame" in cols:
        op.drop_column("product", "in_frame")
    if "quality" in cols:
        op.drop_column("product", "quality")
