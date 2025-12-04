"""add quality to productmatch and overrides table

Revision ID: ed7e4f7c8f2d
Revises: cc0a7f8c2104
Create Date: 2025-12-01 01:20:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "ed7e4f7c8f2d"
down_revision: Union[str, None] = "cc0a7f8c2104"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("productmatch", sa.Column("quality", sa.String(length=50), nullable=True))
    op.create_table(
        "productmatchoverride",
        sa.Column("competitor_source", sa.String(length=128), nullable=False),
        sa.Column("competitor_sku", sa.String(length=128), nullable=True),
        sa.Column("brand", sa.String(length=100), nullable=True),
        sa.Column("model", sa.String(length=150), nullable=True),
        sa.Column("variant", sa.String(length=50), nullable=True),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("phone_model_id", sa.Integer(), nullable=True),
        sa.Column("quality", sa.String(length=50), nullable=True),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(length=100), nullable=True),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.ForeignKeyConstraint(["phone_model_id"], ["phone_models.id"]),
        sa.ForeignKeyConstraint(["product_id"], ["product.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("competitor_source", "competitor_sku", name="uq_product_match_override_source_sku"),
    )


def downgrade() -> None:
    op.drop_table("productmatchoverride")
    op.drop_column("productmatch", "quality")

