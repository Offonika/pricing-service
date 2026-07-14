"""add product live candidate cache

Revision ID: 9c0d1e2f3a4b
Revises: 8b9c0d1e2f3a, cb01d2e3f4a5
Create Date: 2026-05-02 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "9c0d1e2f3a4b"
down_revision: str | Sequence[str] | None = ("8b9c0d1e2f3a", "cb01d2e3f4a5")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "product_live_candidate_cache",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("live_candidate_count", sa.Integer(), nullable=False),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["product_id"], ["product.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("product_id", name="uq_product_live_candidate_cache_product"),
    )
    op.create_index(
        "ix_product_live_candidate_cache_product_id",
        "product_live_candidate_cache",
        ["product_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_product_live_candidate_cache_product_id",
        table_name="product_live_candidate_cache",
    )
    op.drop_table("product_live_candidate_cache")
