"""init schema

Revision ID: adc1afd6479a
Revises:
Create Date: 2025-11-23 08:23:38.837955

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "adc1afd6479a"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "competitor",
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("website", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_competitor_name"), "competitor", ["name"], unique=True)

    op.create_table(
        "product",
        sa.Column("sku", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("brand", sa.String(length=100), nullable=True),
        sa.Column("category", sa.String(length=100), nullable=True),
        sa.Column("abc_class", sa.String(length=5), nullable=True),
        sa.Column("xyz_class", sa.String(length=5), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_product_sku"), "product", ["sku"], unique=True)

    op.create_table(
        "competitorprice",
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("competitor_id", sa.Integer(), nullable=False),
        sa.Column("price", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("in_stock", sa.Boolean(), nullable=False),
        sa.Column(
            "collected_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.ForeignKeyConstraint(["competitor_id"], ["competitor.id"]),
        sa.ForeignKeyConstraint(["product_id"], ["product.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_competitor_price_product_competitor_date",
        "competitorprice",
        ["product_id", "competitor_id", "collected_at"],
        unique=False,
    )
    op.create_index(op.f("ix_competitorprice_competitor_id"), "competitorprice", ["competitor_id"], unique=False)
    op.create_index(op.f("ix_competitorprice_product_id"), "competitorprice", ["product_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_competitorprice_product_id"), table_name="competitorprice")
    op.drop_index(op.f("ix_competitorprice_competitor_id"), table_name="competitorprice")
    op.drop_index("ix_competitor_price_product_competitor_date", table_name="competitorprice")
    op.drop_table("competitorprice")
    op.drop_index(op.f("ix_product_sku"), table_name="product")
    op.drop_table("product")
    op.drop_index(op.f("ix_competitor_name"), table_name="competitor")
    op.drop_table("competitor")
