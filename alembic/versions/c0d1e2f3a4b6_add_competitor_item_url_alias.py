"""add competitor item url aliases

Revision ID: c0d1e2f3a4b6
Revises: b1c2d3e4f6a8
Create Date: 2026-05-09 02:05:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c0d1e2f3a4b6"
down_revision: str | None = "b1c2d3e4f6a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if insp.has_table("competitor_item_url_alias"):
        return

    op.create_table(
        "competitor_item_url_alias",
        sa.Column("competitor_item_id", sa.Integer(), nullable=False),
        sa.Column("competitor", sa.String(length=128), nullable=False),
        sa.Column("alias_url", sa.String(length=1024), nullable=False),
        sa.Column("normalized_url", sa.String(length=1024), nullable=False),
        sa.Column("url_kind", sa.String(length=32), nullable=False),
        sa.Column("catalog_id", sa.String(length=64), nullable=True),
        sa.Column("redirect_id", sa.String(length=64), nullable=True),
        sa.Column("resolved_from_url", sa.String(length=1024), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
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
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.ForeignKeyConstraint(
            ["competitor_item_id"],
            ["competitor_item.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "competitor",
            "normalized_url",
            name="uq_competitor_item_url_alias_competitor_url",
        ),
    )
    op.create_index(
        op.f("ix_competitor_item_url_alias_catalog_id"),
        "competitor_item_url_alias",
        ["catalog_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_competitor_item_url_alias_competitor"),
        "competitor_item_url_alias",
        ["competitor"],
        unique=False,
    )
    op.create_index(
        op.f("ix_competitor_item_url_alias_competitor_item_id"),
        "competitor_item_url_alias",
        ["competitor_item_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_competitor_item_url_alias_normalized_url"),
        "competitor_item_url_alias",
        ["normalized_url"],
        unique=False,
    )
    op.create_index(
        op.f("ix_competitor_item_url_alias_redirect_id"),
        "competitor_item_url_alias",
        ["redirect_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_competitor_item_url_alias_redirect_id"),
        table_name="competitor_item_url_alias",
    )
    op.drop_index(
        op.f("ix_competitor_item_url_alias_normalized_url"),
        table_name="competitor_item_url_alias",
    )
    op.drop_index(
        op.f("ix_competitor_item_url_alias_competitor_item_id"),
        table_name="competitor_item_url_alias",
    )
    op.drop_index(
        op.f("ix_competitor_item_url_alias_competitor"),
        table_name="competitor_item_url_alias",
    )
    op.drop_index(
        op.f("ix_competitor_item_url_alias_catalog_id"),
        table_name="competitor_item_url_alias",
    )
    op.drop_table("competitor_item_url_alias")
