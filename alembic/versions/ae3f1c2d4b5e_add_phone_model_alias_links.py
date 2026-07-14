"""add phone model alias and canonical links

Revision ID: ae3f1c2d4b5e
Revises: 8fa1f9bf619d
Create Date: 2026-03-08 14:30:00.000000
"""

from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ae3f1c2d4b5e"
down_revision: str | None = "8fa1f9bf619d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "phone_model_alias",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("phone_model_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("raw_value", sa.String(length=255), nullable=False),
        sa.Column("raw_brand", sa.String(length=100), nullable=True),
        sa.Column("raw_model", sa.String(length=150), nullable=True),
        sa.Column("raw_variant", sa.String(length=50), nullable=True),
        sa.Column("normalized_key", sa.String(length=255), nullable=False),
        sa.Column("confidence", sa.Numeric(4, 3), nullable=True),
        sa.Column("is_manual", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["phone_model_id"], ["phone_models.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source",
            "normalized_key",
            "phone_model_id",
            name="uq_phone_model_alias_source_key_model",
        ),
    )
    op.create_index("ix_phone_model_alias_phone_model_id", "phone_model_alias", ["phone_model_id"])
    op.create_index("ix_phone_model_alias_source", "phone_model_alias", ["source"])
    op.create_index("ix_phone_model_alias_normalized_key", "phone_model_alias", ["normalized_key"])

    op.create_table(
        "product_phone_model",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("phone_model_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("raw_value", sa.String(length=255), nullable=True),
        sa.Column("confidence", sa.Numeric(4, 3), nullable=True),
        sa.Column("is_manual", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["phone_model_id"], ["phone_models.id"]),
        sa.ForeignKeyConstraint(["product_id"], ["product.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "product_id",
            "phone_model_id",
            "source",
            name="uq_product_phone_model_product_model_source",
        ),
    )
    op.create_index("ix_product_phone_model_product_id", "product_phone_model", ["product_id"])
    op.create_index(
        "ix_product_phone_model_phone_model_id", "product_phone_model", ["phone_model_id"]
    )
    op.create_index("ix_product_phone_model_source", "product_phone_model", ["source"])

    with op.batch_alter_table("competitor_item_compatibility") as batch_op:
        batch_op.add_column(sa.Column("phone_model_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_competitor_item_compatibility_phone_model_id",
            "phone_models",
            ["phone_model_id"],
            ["id"],
        )
        batch_op.create_index("ix_competitor_item_compatibility_phone_model_id", ["phone_model_id"])


def downgrade() -> None:
    with op.batch_alter_table("competitor_item_compatibility") as batch_op:
        batch_op.drop_index("ix_competitor_item_compatibility_phone_model_id")
        batch_op.drop_constraint(
            "fk_competitor_item_compatibility_phone_model_id", type_="foreignkey"
        )
        batch_op.drop_column("phone_model_id")

    op.drop_index("ix_product_phone_model_source", table_name="product_phone_model")
    op.drop_index("ix_product_phone_model_phone_model_id", table_name="product_phone_model")
    op.drop_index("ix_product_phone_model_product_id", table_name="product_phone_model")
    op.drop_table("product_phone_model")

    op.drop_index("ix_phone_model_alias_normalized_key", table_name="phone_model_alias")
    op.drop_index("ix_phone_model_alias_source", table_name="phone_model_alias")
    op.drop_index("ix_phone_model_alias_phone_model_id", table_name="phone_model_alias")
    op.drop_table("phone_model_alias")
