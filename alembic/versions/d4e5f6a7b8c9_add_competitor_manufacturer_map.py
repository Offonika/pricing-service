"""add competitor manufacturer mapping table

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2025-02-06 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "d4e5f6a7b8c9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "competitor_manufacturer_map",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("competitor", sa.String(length=128), nullable=False),
        sa.Column("raw_label", sa.String(length=128), nullable=False),
        sa.Column("normalized_manufacturer", sa.String(length=128), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("competitor", "raw_label", name="uq_competitor_manufacturer_map"),
    )
    op.create_index(
        "ix_competitor_manufacturer_map_competitor",
        "competitor_manufacturer_map",
        ["competitor"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_competitor_manufacturer_map_competitor", table_name="competitor_manufacturer_map"
    )
    op.drop_table("competitor_manufacturer_map")
