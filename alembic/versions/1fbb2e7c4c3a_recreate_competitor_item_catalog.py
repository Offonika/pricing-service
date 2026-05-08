"""recreate competitor item catalog to align schema

Revision ID: 1fbb2e7c4c3a
Revises: f3c1e9d2b7aa
Create Date: 2025-12-16 21:00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1fbb2e7c4c3a"
down_revision: str | None = "f3c1e9d2b7aa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)

    if insp.has_table("competitor_item_snapshot"):
        op.drop_table("competitor_item_snapshot")
    if insp.has_table("competitor_item"):
        op.drop_table("competitor_item")

    op.create_table(
        "competitor_item",
        sa.Column("competitor", sa.String(length=128), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=1024), nullable=True),
        sa.Column("category", sa.String(length=255), nullable=True),
        sa.Column("url", sa.String(length=1024), nullable=True),
        sa.Column("name_norm", sa.String(length=1024), nullable=True),
        sa.Column("sku_norm", sa.String(length=255), nullable=True),
        sa.Column("price_opt", sa.Numeric(12, 2), nullable=True),
        sa.Column("price_roz", sa.Numeric(12, 2), nullable=True),
        sa.Column("availability", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("scraped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("parsed_brand", sa.String(length=128), nullable=True),
        sa.Column("parsed_model", sa.String(length=255), nullable=True),
        sa.Column("parsed_variant", sa.String(length=50), nullable=True),
        sa.Column("parse_confidence", sa.Numeric(4, 3), nullable=True),
        sa.Column("parse_notes", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "competitor", "external_id", name="uq_competitor_item_competitor_external"
        ),
    )
    op.create_index(
        op.f("ix_competitor_item_competitor"), "competitor_item", ["competitor"], unique=False
    )
    op.create_index(
        op.f("ix_competitor_item_is_active"), "competitor_item", ["is_active"], unique=False
    )
    op.create_index(
        op.f("ix_competitor_item_parsed_brand"), "competitor_item", ["parsed_brand"], unique=False
    )
    op.create_index(
        op.f("ix_competitor_item_sku_norm"), "competitor_item", ["sku_norm"], unique=False
    )

    op.create_table(
        "competitor_item_snapshot",
        sa.Column("competitor_item_id", sa.Integer(), nullable=False),
        sa.Column("price_roz", sa.Numeric(12, 2), nullable=True),
        sa.Column("price_opt", sa.Numeric(12, 2), nullable=True),
        sa.Column("availability", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "scraped_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.ForeignKeyConstraint(["competitor_item_id"], ["competitor_item.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_competitor_item_snapshot_competitor_item_id"),
        "competitor_item_snapshot",
        ["competitor_item_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_competitor_item_snapshot_competitor_item_id"),
        table_name="competitor_item_snapshot",
    )
    op.drop_table("competitor_item_snapshot")
    op.drop_index(op.f("ix_competitor_item_sku_norm"), table_name="competitor_item")
    op.drop_index(op.f("ix_competitor_item_parsed_brand"), table_name="competitor_item")
    op.drop_index(op.f("ix_competitor_item_is_active"), table_name="competitor_item")
    op.drop_index(op.f("ix_competitor_item_competitor"), table_name="competitor_item")
    op.drop_table("competitor_item")
