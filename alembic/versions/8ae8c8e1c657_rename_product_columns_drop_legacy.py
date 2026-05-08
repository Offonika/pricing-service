"""rename product columns and drop legacy fields

Revision ID: 8ae8c8e1c657
Revises: 6f0f07a5189a
Create Date: 2025-12-07 11:25:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8ae8c8e1c657"
down_revision: str | None = "6f0f07a5189a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


LEGACY_COLUMNS = ("abc_class", "xyz_class", "subject", "quality", "in_frame", "display_type")


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    indexes = {ix["name"] for ix in insp.get_indexes("product")}

    op.execute("DROP VIEW IF EXISTS vw_match_ambiguous_with_candidates")
    op.execute("DROP VIEW IF EXISTS vw_product_no_competitor_reasons")
    op.execute("DROP VIEW IF EXISTS vw_product_competitor_candidates")

    if "ix_product_sku" in indexes:
        op.drop_index("ix_product_sku", table_name="product")
    if "ix_product_one_c_code" in indexes:
        op.drop_index("ix_product_one_c_code", table_name="product")

    with op.batch_alter_table("product") as batch:
        cols = {c["name"] for c in insp.get_columns("product")}
        if "sku" in cols:
            batch.alter_column("sku", new_column_name="article")
        if "one_c_code" in cols:
            batch.alter_column("one_c_code", new_column_name="code_1c")
        for col in LEGACY_COLUMNS:
            if col in cols:
                batch.drop_column(col)

    op.create_index(op.f("ix_product_article"), "product", ["article"], unique=True)
    op.create_index(op.f("ix_product_code_1c"), "product", ["code_1c"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    indexes = {ix["name"] for ix in insp.get_indexes("product")}

    if "ix_product_code_1c" in indexes:
        op.drop_index("ix_product_code_1c", table_name="product")
    if "ix_product_article" in indexes:
        op.drop_index("ix_product_article", table_name="product")

    with op.batch_alter_table("product") as batch:
        cols = {c["name"] for c in insp.get_columns("product")}
        if "article" in cols:
            batch.alter_column("article", new_column_name="sku")
        if "code_1c" in cols:
            batch.alter_column("code_1c", new_column_name="one_c_code")
        for col in LEGACY_COLUMNS:
            if col not in cols:
                if col in {"abc_class", "xyz_class"}:
                    batch.add_column(sa.Column(col, sa.String(length=5), nullable=True))
                elif col == "subject":
                    batch.add_column(sa.Column(col, sa.String(length=100), nullable=True))
                elif col in {"quality", "in_frame", "display_type"}:
                    batch.add_column(sa.Column(col, sa.Text(), nullable=True))

    op.create_index(op.f("ix_product_sku"), "product", ["sku"], unique=True)
    op.create_index(op.f("ix_product_one_c_code"), "product", ["one_c_code"], unique=False)
