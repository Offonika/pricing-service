"""add raw quality columns to product

Revision ID: e6b4a2c9d111
Revises: c4f2a1d9e8b3
Create Date: 2026-03-12 11:40:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e6b4a2c9d111"
down_revision: str | None = "c4f2a1d9e8b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("product")}

    with op.batch_alter_table("product") as batch:
        if "quality_raw" not in cols:
            batch.add_column(sa.Column("quality_raw", sa.String(length=100), nullable=True))
        if "display_quality_raw" not in cols:
            batch.add_column(sa.Column("display_quality_raw", sa.String(length=100), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("product")}

    with op.batch_alter_table("product") as batch:
        if "display_quality_raw" in cols:
            batch.drop_column("display_quality_raw")
        if "quality_raw" in cols:
            batch.drop_column("quality_raw")
