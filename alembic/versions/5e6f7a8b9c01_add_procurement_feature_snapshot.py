"""add procurement feature snapshot to assortment classification

Revision ID: 5e6f7a8b9c01
Revises: 4e5f6a7b8c90
Create Date: 2026-07-03 19:20:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "5e6f7a8b9c01"
down_revision = "4e5f6a7b8c90"
branch_labels = None
depends_on = None


def upgrade() -> None:
    table_name = "assortment_lifecycle_classification"
    op.add_column(
        table_name,
        sa.Column(
            "feature_snapshot_schema", sa.String(length=64), nullable=False, server_default=""
        ),
    )
    op.add_column(
        table_name,
        sa.Column("product_ref", sa.String(length=64), nullable=False, server_default=""),
    )
    op.add_column(
        table_name, sa.Column("article", sa.String(length=128), nullable=False, server_default="")
    )
    op.add_column(
        table_name, sa.Column("kind_1c", sa.String(length=128), nullable=False, server_default="")
    )
    op.add_column(
        table_name,
        sa.Column("subject_1c", sa.String(length=255), nullable=False, server_default=""),
    )
    op.add_column(
        table_name,
        sa.Column("category_1c", sa.String(length=255), nullable=False, server_default=""),
    )
    op.add_column(
        table_name,
        sa.Column("item_tags", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )
    op.add_column(
        table_name,
        sa.Column("brand_compatibility", sa.String(length=255), nullable=False, server_default=""),
    )
    op.add_column(
        table_name,
        sa.Column("model_compatibility", sa.String(length=255), nullable=False, server_default=""),
    )
    op.add_column(
        table_name,
        sa.Column("quality_raw", sa.String(length=255), nullable=False, server_default=""),
    )
    op.add_column(
        table_name,
        sa.Column("quality_normalized", sa.String(length=128), nullable=False, server_default=""),
    )
    op.add_column(
        table_name,
        sa.Column(
            "characteristic_values", sa.JSON(), nullable=False, server_default=sa.text("'{}'")
        ),
    )
    op.add_column(
        table_name,
        sa.Column("price_segment", sa.String(length=64), nullable=False, server_default=""),
    )
    op.add_column(
        table_name,
        sa.Column("data_quality_score", sa.String(length=16), nullable=False, server_default=""),
    )
    op.add_column(
        table_name,
        sa.Column(
            "missing_required_attributes",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )
    op.add_column(
        table_name,
        sa.Column(
            "future_ka_mapping_status", sa.String(length=32), nullable=False, server_default=""
        ),
    )
    op.add_column(
        table_name,
        sa.Column(
            "calculation_unit_level", sa.String(length=64), nullable=False, server_default=""
        ),
    )
    op.add_column(
        table_name,
        sa.Column("calculation_unit_key", sa.String(length=512), nullable=False, server_default=""),
    )
    op.add_column(
        table_name,
        sa.Column(
            "calculation_unit_source", sa.String(length=64), nullable=False, server_default=""
        ),
    )
    op.add_column(
        table_name,
        sa.Column(
            "calculation_unit_confidence", sa.String(length=16), nullable=False, server_default=""
        ),
    )
    op.add_column(
        table_name,
        sa.Column("calculation_unit_reason", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        table_name,
        sa.Column("demand_method_code", sa.String(length=64), nullable=False, server_default=""),
    )
    op.add_column(
        table_name, sa.Column("demand_method_reason", sa.Text(), nullable=False, server_default="")
    )
    op.add_column(
        table_name,
        sa.Column(
            "demand_method_confidence", sa.String(length=16), nullable=False, server_default=""
        ),
    )
    op.create_index(
        "ix_assortment_lifecycle_classification_ka_mapping",
        table_name,
        ["future_ka_mapping_status"],
    )
    op.create_index(
        "ix_assortment_lifecycle_classification_unit_level",
        table_name,
        ["calculation_unit_level"],
    )
    op.create_index(
        "ix_assortment_lifecycle_classification_demand_method",
        table_name,
        ["demand_method_code"],
    )


def downgrade() -> None:
    table_name = "assortment_lifecycle_classification"
    op.drop_index("ix_assortment_lifecycle_classification_demand_method", table_name=table_name)
    op.drop_index("ix_assortment_lifecycle_classification_unit_level", table_name=table_name)
    op.drop_index("ix_assortment_lifecycle_classification_ka_mapping", table_name=table_name)
    op.drop_column(table_name, "demand_method_confidence")
    op.drop_column(table_name, "demand_method_reason")
    op.drop_column(table_name, "demand_method_code")
    op.drop_column(table_name, "calculation_unit_reason")
    op.drop_column(table_name, "calculation_unit_confidence")
    op.drop_column(table_name, "calculation_unit_source")
    op.drop_column(table_name, "calculation_unit_key")
    op.drop_column(table_name, "calculation_unit_level")
    op.drop_column(table_name, "future_ka_mapping_status")
    op.drop_column(table_name, "missing_required_attributes")
    op.drop_column(table_name, "data_quality_score")
    op.drop_column(table_name, "price_segment")
    op.drop_column(table_name, "characteristic_values")
    op.drop_column(table_name, "quality_normalized")
    op.drop_column(table_name, "quality_raw")
    op.drop_column(table_name, "model_compatibility")
    op.drop_column(table_name, "brand_compatibility")
    op.drop_column(table_name, "item_tags")
    op.drop_column(table_name, "category_1c")
    op.drop_column(table_name, "subject_1c")
    op.drop_column(table_name, "kind_1c")
    op.drop_column(table_name, "article")
    op.drop_column(table_name, "product_ref")
    op.drop_column(table_name, "feature_snapshot_schema")
