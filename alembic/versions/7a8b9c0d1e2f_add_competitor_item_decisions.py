"""add competitor item decision log

Revision ID: 7a8b9c0d1e2f
Revises: 6d5c4b3a2f10
Create Date: 2026-05-01 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "7a8b9c0d1e2f"
down_revision: str | Sequence[str] | None = "6d5c4b3a2f10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "product_competitor_item_decision" not in tables:
        op.create_table(
            "product_competitor_item_decision",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("product_id", sa.Integer(), nullable=False),
            sa.Column("competitor_item_id", sa.Integer(), nullable=False),
            sa.Column("action", sa.String(length=32), nullable=False),
            sa.Column("reason", sa.String(length=500), nullable=True),
            sa.Column("created_by", sa.String(length=100), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("previous_product_id", sa.Integer(), nullable=True),
            sa.Column("previous_status", sa.String(length=32), nullable=True),
            sa.ForeignKeyConstraint(
                ["competitor_item_id"], ["competitor_item.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(["product_id"], ["product.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_product_competitor_item_decision_product_id",
            "product_competitor_item_decision",
            ["product_id"],
        )
        op.create_index(
            "ix_product_competitor_item_decision_competitor_item_id",
            "product_competitor_item_decision",
            ["competitor_item_id"],
        )
        op.create_index(
            "ix_product_competitor_item_decision_action",
            "product_competitor_item_decision",
            ["action"],
        )
        op.create_index(
            "ix_product_competitor_item_decision_created_at",
            "product_competitor_item_decision",
            ["created_at"],
        )

    if bind.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        existing_indexes = {idx["name"] for idx in inspector.get_indexes("competitor_item")}
        if "ix_competitor_item_name_trgm" not in existing_indexes:
            op.create_index(
                "ix_competitor_item_name_trgm",
                "competitor_item",
                ["name"],
                postgresql_using="gin",
                postgresql_ops={"name": "gin_trgm_ops"},
            )
        if "ix_competitor_item_normalized_title_trgm" not in existing_indexes:
            op.create_index(
                "ix_competitor_item_normalized_title_trgm",
                "competitor_item",
                ["normalized_title"],
                postgresql_using="gin",
                postgresql_ops={"normalized_title": "gin_trgm_ops"},
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_indexes = {idx["name"] for idx in inspector.get_indexes("competitor_item")}
    if "ix_competitor_item_normalized_title_trgm" in existing_indexes:
        op.drop_index("ix_competitor_item_normalized_title_trgm", table_name="competitor_item")
    if "ix_competitor_item_name_trgm" in existing_indexes:
        op.drop_index("ix_competitor_item_name_trgm", table_name="competitor_item")

    if "product_competitor_item_decision" in set(inspector.get_table_names()):
        op.drop_index(
            "ix_product_competitor_item_decision_created_at",
            table_name="product_competitor_item_decision",
        )
        op.drop_index(
            "ix_product_competitor_item_decision_action",
            table_name="product_competitor_item_decision",
        )
        op.drop_index(
            "ix_product_competitor_item_decision_competitor_item_id",
            table_name="product_competitor_item_decision",
        )
        op.drop_index(
            "ix_product_competitor_item_decision_product_id",
            table_name="product_competitor_item_decision",
        )
        op.drop_table("product_competitor_item_decision")
