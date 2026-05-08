"""add marked flag to product

Revision ID: 6f0f07a5189a
Revises: 4a941de3de6b
Create Date: 2025-12-07 11:18:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6f0f07a5189a"
down_revision: str | None = "4a941de3de6b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    cols = {c["name"] for c in insp.get_columns("product")}
    if "is_marked_for_deletion" not in cols:
        op.add_column(
            "product",
            sa.Column(
                "is_marked_for_deletion", sa.Boolean(), nullable=False, server_default=sa.false()
            ),
        )
        op.execute(sa.text("ALTER TABLE product ALTER COLUMN is_marked_for_deletion DROP DEFAULT"))


def downgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    cols = {c["name"] for c in insp.get_columns("product")}
    if "is_marked_for_deletion" in cols:
        op.drop_column("product", "is_marked_for_deletion")
