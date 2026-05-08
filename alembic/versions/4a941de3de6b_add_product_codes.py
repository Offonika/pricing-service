"""add 1c and info system codes to product

Revision ID: 4a941de3de6b
Revises: d7a03a2f7201
Create Date: 2025-12-10 12:00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4a941de3de6b"
down_revision: str | None = "d7a03a2f7201"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    cols = {c["name"] for c in insp.get_columns("product")}
    indexes = {ix["name"] for ix in insp.get_indexes("product")}

    if "one_c_code" not in cols:
        op.add_column("product", sa.Column("one_c_code", sa.String(length=100), nullable=True))
    if "info_system_code" not in cols:
        op.add_column(
            "product", sa.Column("info_system_code", sa.String(length=100), nullable=True)
        )

    if "ix_product_one_c_code" not in indexes:
        op.create_index(op.f("ix_product_one_c_code"), "product", ["one_c_code"], unique=False)
    if "ix_product_info_system_code" not in indexes:
        op.create_index(
            op.f("ix_product_info_system_code"), "product", ["info_system_code"], unique=False
        )


def downgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    cols = {c["name"] for c in insp.get_columns("product")}
    indexes = {ix["name"] for ix in insp.get_indexes("product")}

    if "ix_product_info_system_code" in indexes:
        op.drop_index(op.f("ix_product_info_system_code"), table_name="product")
    if "ix_product_one_c_code" in indexes:
        op.drop_index(op.f("ix_product_one_c_code"), table_name="product")

    if "info_system_code" in cols:
        op.drop_column("product", "info_system_code")
    if "one_c_code" in cols:
        op.drop_column("product", "one_c_code")
