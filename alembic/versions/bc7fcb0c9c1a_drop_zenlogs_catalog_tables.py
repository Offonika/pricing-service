"""drop zenlogs catalog tables

Revision ID: bc7fcb0c9c1a
Revises: a3f6a9f4d2b1
Create Date: 2025-11-30 13:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "bc7fcb0c9c1a"
down_revision: Union[str, None] = "a3f6a9f4d2b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_table("competitor_item_snapshot")
    op.drop_table("competitor_item")


def downgrade() -> None:
    op.create_table(
        "competitor_item",
        sa.Column("competitor_id", sa.Integer(), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=1024), nullable=True),
        sa.Column("category", sa.String(length=255), nullable=True),
        sa.Column("url", sa.String(length=1024), nullable=True),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.ForeignKeyConstraint(["competitor_id"], ["competitor.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "competitor_id",
            "external_id",
            name="uq_competitor_item_competitor_external",
        ),
    )
    op.create_index(
        op.f("ix_competitor_item_competitor_id"),
        "competitor_item",
        ["competitor_id"],
        unique=False,
    )
    op.create_table(
        "competitor_item_snapshot",
        sa.Column("competitor_item_id", sa.Integer(), nullable=False),
        sa.Column("price_roz", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("price_opt", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("availability", sa.Boolean(), nullable=False),
        sa.Column(
            "scraped_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.ForeignKeyConstraint(["competitor_item_id"], ["competitor_item.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_competitor_item_snapshot_competitor_item_id"),
        "competitor_item_snapshot",
        ["competitor_item_id"],
        unique=False,
    )
