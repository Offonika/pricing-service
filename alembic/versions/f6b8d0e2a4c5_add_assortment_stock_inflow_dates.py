"""add assortment physical stock inflow dates

Revision ID: f6b8d0e2a4c5
Revises: e5a7c9d1f3b4
Create Date: 2026-08-13 15:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "f6b8d0e2a4c5"
down_revision = "e5a7c9d1f3b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "assortment_lifecycle_classification",
        sa.Column("first_stock_inflow_at", sa.Date(), nullable=True),
    )
    op.add_column(
        "assortment_lifecycle_classification",
        sa.Column("last_stock_inflow_at", sa.Date(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("assortment_lifecycle_classification", "last_stock_inflow_at")
    op.drop_column("assortment_lifecycle_classification", "first_stock_inflow_at")
