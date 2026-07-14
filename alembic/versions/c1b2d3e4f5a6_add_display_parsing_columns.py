"""add display parsing columns to competitor_item

Revision ID: c1b2d3e4f5a6
Revises: ab12cd34ef56
Create Date: 2026-01-20 10:00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c1b2d3e4f5a6"
down_revision: str | None = "ab12cd34ef56"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "competitor_item",
        sa.Column("screen_matrix_type", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "competitor_item",
        sa.Column("screen_kit", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "competitor_item",
        sa.Column("backlight", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "competitor_item",
        sa.Column("screen_construction", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "competitor_item",
        sa.Column("screen_quality_grade", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "competitor_item",
        sa.Column("refresh_rate_hz", sa.Integer(), nullable=True),
    )
    op.add_column(
        "competitor_item",
        sa.Column("oleophobic", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "competitor_item",
        sa.Column("has_frame", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "competitor_item",
        sa.Column("color", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "competitor_item",
        sa.Column(
            "notes_raw_tokens",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
    )

    op.execute(
        "UPDATE competitor_item SET refresh_rate_hz = attrs_refresh_rate_hz "
        "WHERE refresh_rate_hz IS NULL AND attrs_refresh_rate_hz IS NOT NULL"
    )
    op.execute(
        "UPDATE competitor_item SET color = attrs_color "
        "WHERE color IS NULL AND attrs_color IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_column("competitor_item", "notes_raw_tokens")
    op.drop_column("competitor_item", "color")
    op.drop_column("competitor_item", "has_frame")
    op.drop_column("competitor_item", "oleophobic")
    op.drop_column("competitor_item", "refresh_rate_hz")
    op.drop_column("competitor_item", "screen_quality_grade")
    op.drop_column("competitor_item", "screen_construction")
    op.drop_column("competitor_item", "backlight")
    op.drop_column("competitor_item", "screen_kit")
    op.drop_column("competitor_item", "screen_matrix_type")
