"""widen assortment feature text fields

Revision ID: 6f7a8b9c0d12
Revises: 5e6f7a8b9c01
Create Date: 2026-07-03 19:52:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "6f7a8b9c0d12"
down_revision = "5e6f7a8b9c01"
branch_labels = None
depends_on = None


TABLE_NAME = "assortment_lifecycle_classification"


def upgrade() -> None:
    for column_name in (
        "name",
        "folder",
        "kind_1c",
        "subject_1c",
        "category_1c",
        "brand_compatibility",
        "model_compatibility",
        "quality_raw",
        "calculation_unit_key",
    ):
        op.alter_column(
            TABLE_NAME,
            column_name,
            existing_nullable=False,
            type_=sa.Text(),
            postgresql_using=f"{column_name}::text",
        )


def downgrade() -> None:
    op.alter_column(
        TABLE_NAME,
        "calculation_unit_key",
        existing_nullable=False,
        type_=sa.String(length=512),
        postgresql_using="left(calculation_unit_key, 512)",
    )
    for column_name in (
        "quality_raw",
        "model_compatibility",
        "brand_compatibility",
        "category_1c",
        "subject_1c",
        "name",
    ):
        op.alter_column(
            TABLE_NAME,
            column_name,
            existing_nullable=False,
            type_=sa.String(length=255),
            postgresql_using=f"left({column_name}, 255)",
        )
    for column_name in ("kind_1c",):
        op.alter_column(
            TABLE_NAME,
            column_name,
            existing_nullable=False,
            type_=sa.String(length=128),
            postgresql_using=f"left({column_name}, 128)",
        )
    op.alter_column(
        TABLE_NAME,
        "folder",
        existing_nullable=False,
        type_=sa.String(length=512),
        postgresql_using="left(folder, 512)",
    )
