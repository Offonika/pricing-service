"""add product sku fields and 1c properties for sku generation

Revision ID: b7d44f67a112
Revises: ae3f1c2d4b5e
Create Date: 2026-03-11 13:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7d44f67a112"
down_revision: str | None = "ae3f1c2d4b5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("product")}
    indexes = {ix["name"] for ix in insp.get_indexes("product")}

    with op.batch_alter_table("product") as batch:
        if "sku" not in cols:
            batch.add_column(sa.Column("sku", sa.String(length=35), nullable=True))
        if "sku_brand_code" not in cols:
            batch.add_column(sa.Column("sku_brand_code", sa.String(length=8), nullable=True))
        if "sku_category_code" not in cols:
            batch.add_column(sa.Column("sku_category_code", sa.String(length=8), nullable=True))
        if "sku_device_code" not in cols:
            batch.add_column(sa.Column("sku_device_code", sa.String(length=32), nullable=True))
        if "sku_key" not in cols:
            batch.add_column(sa.Column("sku_key", sa.String(length=64), nullable=True))
        if "sku_rev" not in cols:
            batch.add_column(sa.Column("sku_rev", sa.String(length=16), nullable=True))
        if "sku_status" not in cols:
            batch.add_column(sa.Column("sku_status", sa.String(length=32), nullable=True))
        if "sku_error" not in cols:
            batch.add_column(sa.Column("sku_error", sa.String(length=255), nullable=True))
        if "battery_capacity_mah" not in cols:
            batch.add_column(sa.Column("battery_capacity_mah", sa.Integer(), nullable=True))
        if "battery_is_high_capacity" not in cols:
            batch.add_column(sa.Column("battery_is_high_capacity", sa.Boolean(), nullable=True))
        if "battery_voltage" not in cols:
            batch.add_column(sa.Column("battery_voltage", sa.String(length=32), nullable=True))
        if "battery_energy_wh" not in cols:
            batch.add_column(sa.Column("battery_energy_wh", sa.String(length=32), nullable=True))
        if "cable_connector_input" not in cols:
            batch.add_column(
                sa.Column("cable_connector_input", sa.String(length=50), nullable=True)
            )
        if "cable_connector_output" not in cols:
            batch.add_column(
                sa.Column("cable_connector_output", sa.String(length=50), nullable=True)
            )
        if "cable_length" not in cols:
            batch.add_column(sa.Column("cable_length", sa.String(length=50), nullable=True))
        if "charger_power_w" not in cols:
            batch.add_column(sa.Column("charger_power_w", sa.Integer(), nullable=True))
        if "charger_technology" not in cols:
            batch.add_column(sa.Column("charger_technology", sa.String(length=50), nullable=True))
        if "charger_plug_type" not in cols:
            batch.add_column(sa.Column("charger_plug_type", sa.String(length=50), nullable=True))
        if "camera_position" not in cols:
            batch.add_column(sa.Column("camera_position", sa.String(length=20), nullable=True))
        if "camera_megapixels" not in cols:
            batch.add_column(sa.Column("camera_megapixels", sa.Integer(), nullable=True))
        if "flex_purpose" not in cols:
            batch.add_column(sa.Column("flex_purpose", sa.String(length=100), nullable=True))
        if "glass_type" not in cols:
            batch.add_column(sa.Column("glass_type", sa.String(length=50), nullable=True))
        if "glass_form" not in cols:
            batch.add_column(sa.Column("glass_form", sa.String(length=50), nullable=True))
        if "chip_code" not in cols:
            batch.add_column(sa.Column("chip_code", sa.String(length=100), nullable=True))
        if "part_type" not in cols:
            batch.add_column(sa.Column("part_type", sa.String(length=100), nullable=True))
        if "set_composition" not in cols:
            batch.add_column(sa.Column("set_composition", sa.String(length=255), nullable=True))
        if "set_quantity" not in cols:
            batch.add_column(sa.Column("set_quantity", sa.Integer(), nullable=True))

    if "ix_product_sku" not in indexes:
        op.create_index(op.f("ix_product_sku"), "product", ["sku"], unique=True)


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    indexes = {ix["name"] for ix in insp.get_indexes("product")}
    cols = {c["name"] for c in insp.get_columns("product")}

    if "ix_product_sku" in indexes:
        op.drop_index(op.f("ix_product_sku"), table_name="product")

    with op.batch_alter_table("product") as batch:
        for col in (
            "set_quantity",
            "set_composition",
            "part_type",
            "chip_code",
            "glass_form",
            "glass_type",
            "flex_purpose",
            "camera_megapixels",
            "camera_position",
            "charger_plug_type",
            "charger_technology",
            "charger_power_w",
            "cable_length",
            "cable_connector_output",
            "cable_connector_input",
            "battery_energy_wh",
            "battery_voltage",
            "battery_is_high_capacity",
            "battery_capacity_mah",
            "sku_error",
            "sku_status",
            "sku_rev",
            "sku_key",
            "sku_device_code",
            "sku_category_code",
            "sku_brand_code",
            "sku",
        ):
            if col in cols:
                batch.drop_column(col)
