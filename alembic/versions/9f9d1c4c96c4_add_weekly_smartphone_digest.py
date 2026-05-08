"""add weekly smartphone digest table"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9f9d1c4c96c4"
down_revision: str | None = "1f2c9c8e3e1b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    jsonb_type = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")

    op.create_table(
        "weekly_smartphone_digest",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("week_end", sa.Date(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=True),
        sa.Column("prompt", sa.Text(), nullable=True),
        sa.Column("prompt_chars", sa.Integer(), nullable=True),
        sa.Column("release_ids", jsonb_type, nullable=True),
        sa.Column("stats", jsonb_type, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("week_start", "week_end", name="uq_weekly_smartphone_digest_period"),
    )
    op.create_index(
        "ix_weekly_smartphone_digest_week_end",
        "weekly_smartphone_digest",
        ["week_end"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_weekly_smartphone_digest_week_end", table_name="weekly_smartphone_digest")
    op.drop_table("weekly_smartphone_digest")
