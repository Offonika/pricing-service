"""add structured competitor matching decision feedback

Revision ID: c8d9e0f1a2b3
Revises: f3a4b5c6d7e9
Create Date: 2026-07-31
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "c8d9e0f1a2b3"
down_revision = "f3a4b5c6d7e9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "product_competitor_item_decision",
        sa.Column(
            "reason_code",
            sa.String(length=64),
            nullable=False,
            server_default="legacy_unspecified",
        ),
    )
    op.add_column(
        "product_competitor_item_decision",
        sa.Column(
            "snapshot_json",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_product_competitor_item_decision_reason_code",
        "product_competitor_item_decision",
        ["reason_code"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_product_competitor_item_decision_reason_code",
        table_name="product_competitor_item_decision",
    )
    op.drop_column("product_competitor_item_decision", "snapshot_json")
    op.drop_column("product_competitor_item_decision", "reason_code")
