"""add phone_model_id to product_match

Revision ID: cc0a7f8c2104
Revises: bc7fcb0c9c1a
Create Date: 2025-12-01 01:07:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "cc0a7f8c2104"
down_revision: Union[str, None] = "bc7fcb0c9c1a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("productmatch", sa.Column("phone_model_id", sa.Integer(), nullable=True))
    op.create_index(op.f("ix_productmatch_phone_model_id"), "productmatch", ["phone_model_id"], unique=False)
    op.create_foreign_key(
        "fk_product_match_phone_model",
        "productmatch",
        "phone_models",
        ["phone_model_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_product_match_phone_model", "productmatch", type_="foreignkey")
    op.drop_index(op.f("ix_productmatch_phone_model_id"), table_name="productmatch")
    op.drop_column("productmatch", "phone_model_id")
