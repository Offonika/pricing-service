"""add replacement sku to classification proposal

Revision ID: 1b9d3f5a7c21
Revises: 0a8c2e4f6b7d
Create Date: 2026-08-18 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "1b9d3f5a7c21"
down_revision = "0a8c2e4f6b7d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "procurement_classification_proposal",
        sa.Column("replacement_sku_code", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "procurement_classification_proposal",
        sa.Column("replacement_sku_name", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("procurement_classification_proposal", "replacement_sku_name")
    op.drop_column("procurement_classification_proposal", "replacement_sku_code")
