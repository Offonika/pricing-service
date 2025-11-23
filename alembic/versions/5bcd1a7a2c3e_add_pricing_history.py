"""add pricing history

Revision ID: 5bcd1a7a2c3e
Revises: 1c55b203f5c4
Create Date: 2024-11-29 00:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "5bcd1a7a2c3e"
down_revision: Union[str, None] = "1c55b203f5c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pricingstrategyversion",
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("parameters", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_index(
        op.f("ix_pricingstrategyversion_name"),
        "pricingstrategyversion",
        ["name"],
        unique=True,
    )

    op.create_table(
        "pricerecommendation",
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("strategy_version_id", sa.Integer(), nullable=True),
        sa.Column("recommended_price", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("floor_price", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("competitor_min_price", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("min_margin_pct", sa.Numeric(precision=5, scale=4), nullable=False),
        sa.Column("reasons", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["product.id"],
        ),
        sa.ForeignKeyConstraint(
            ["strategy_version_id"],
            ["pricingstrategyversion.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_pricerecommendation_product_id"),
        "pricerecommendation",
        ["product_id"],
        unique=False,
    )
    op.create_index(
        "ix_price_recommendation_product_created",
        "pricerecommendation",
        ["product_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_price_recommendation_product_created", table_name="pricerecommendation")
    op.drop_index(op.f("ix_pricerecommendation_product_id"), table_name="pricerecommendation")
    op.drop_table("pricerecommendation")
    op.drop_index(op.f("ix_pricingstrategyversion_name"), table_name="pricingstrategyversion")
    op.drop_table("pricingstrategyversion")
