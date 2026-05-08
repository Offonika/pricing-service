"""split product sku into fact and planned sku

Revision ID: c4f2a1d9e8b3
Revises: b7d44f67a112
Create Date: 2026-03-11 15:10:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4f2a1d9e8b3"
down_revision: str | None = "b7d44f67a112"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "product_sku_plan",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("planned_sku", sa.String(length=35), nullable=True),
        sa.Column("brand_code", sa.String(length=8), nullable=True),
        sa.Column("category_code", sa.String(length=8), nullable=True),
        sa.Column("device_code", sa.String(length=32), nullable=True),
        sa.Column("key_code", sa.String(length=64), nullable=True),
        sa.Column("rev", sa.String(length=16), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error_reason", sa.String(length=255), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["product_id"], ["product.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_product_sku_plan_product_id", "product_sku_plan", ["product_id"])
    op.create_index("ix_product_sku_plan_planned_sku", "product_sku_plan", ["planned_sku"])
    op.create_index(
        "uq_product_sku_plan_active_product",
        "product_sku_plan",
        ["product_id"],
        unique=True,
        postgresql_where=sa.text("is_active"),
        sqlite_where=sa.text("is_active = 1"),
    )
    op.create_index(
        "uq_product_sku_plan_active_planned_sku",
        "product_sku_plan",
        ["planned_sku"],
        unique=True,
        postgresql_where=sa.text("is_active AND planned_sku IS NOT NULL"),
        sqlite_where=sa.text("is_active = 1 AND planned_sku IS NOT NULL"),
    )

    with op.batch_alter_table("product") as batch:
        batch.add_column(sa.Column("fact_sku", sa.String(length=35), nullable=True))
        batch.add_column(sa.Column("planned_sku", sa.String(length=35), nullable=True))
        batch.add_column(sa.Column("sku_sync_status", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("sku_sync_error", sa.String(length=255), nullable=True))

    op.create_index(op.f("ix_product_fact_sku"), "product", ["fact_sku"], unique=True)
    op.create_index(op.f("ix_product_planned_sku"), "product", ["planned_sku"], unique=False)

    bind = op.get_bind()
    meta = sa.MetaData()
    product = sa.Table("product", meta, autoload_with=bind)
    plan = sa.Table("product_sku_plan", meta, autoload_with=bind)

    rows = bind.execute(
        sa.select(
            product.c.id,
            product.c.sku,
            product.c.sku_brand_code,
            product.c.sku_category_code,
            product.c.sku_device_code,
            product.c.sku_key,
            product.c.sku_rev,
            product.c.sku_status,
            product.c.sku_error,
        )
    ).mappings()

    for row in rows:
        planned_sku = row["sku"]
        old_status = row["sku_status"]
        if old_status in {"generated"}:
            sync_status = "missing_in_1c" if planned_sku else "missing_plan"
        elif old_status:
            sync_status = "manual_review"
        else:
            sync_status = "missing_plan" if planned_sku is None else "missing_in_1c"

        bind.execute(
            product.update()
            .where(product.c.id == row["id"])
            .values(
                planned_sku=planned_sku,
                sku_sync_status=sync_status,
                sku_sync_error=row["sku_error"],
            )
        )
        if any(
            row[col] is not None
            for col in (
                "sku",
                "sku_brand_code",
                "sku_category_code",
                "sku_device_code",
                "sku_key",
                "sku_rev",
                "sku_status",
                "sku_error",
            )
        ):
            bind.execute(
                plan.insert().values(
                    product_id=row["id"],
                    planned_sku=planned_sku,
                    brand_code=row["sku_brand_code"],
                    category_code=row["sku_category_code"],
                    device_code=row["sku_device_code"],
                    key_code=row["sku_key"],
                    rev=row["sku_rev"],
                    status=old_status or "manual_review",
                    error_reason=row["sku_error"],
                    source="rules",
                    is_active=True,
                )
            )

    with op.batch_alter_table("product") as batch:
        batch.drop_column("sku_error")
        batch.drop_column("sku_status")
        batch.drop_column("sku_rev")
        batch.drop_column("sku_key")
        batch.drop_column("sku_device_code")
        batch.drop_column("sku_category_code")
        batch.drop_column("sku_brand_code")
        batch.drop_column("sku")


def downgrade() -> None:
    with op.batch_alter_table("product") as batch:
        batch.add_column(sa.Column("sku", sa.String(length=35), nullable=True))
        batch.add_column(sa.Column("sku_brand_code", sa.String(length=8), nullable=True))
        batch.add_column(sa.Column("sku_category_code", sa.String(length=8), nullable=True))
        batch.add_column(sa.Column("sku_device_code", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("sku_key", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("sku_rev", sa.String(length=16), nullable=True))
        batch.add_column(sa.Column("sku_status", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("sku_error", sa.String(length=255), nullable=True))

    bind = op.get_bind()
    meta = sa.MetaData()
    product = sa.Table("product", meta, autoload_with=bind)
    plan = sa.Table("product_sku_plan", meta, autoload_with=bind)

    rows = bind.execute(sa.select(plan).where(plan.c.is_active.is_(True))).mappings()
    for row in rows:
        bind.execute(
            product.update()
            .where(product.c.id == row["product_id"])
            .values(
                sku=row["planned_sku"],
                sku_brand_code=row["brand_code"],
                sku_category_code=row["category_code"],
                sku_device_code=row["device_code"],
                sku_key=row["key_code"],
                sku_rev=row["rev"],
                sku_status=row["status"],
                sku_error=row["error_reason"],
            )
        )

    op.drop_index(op.f("ix_product_planned_sku"), table_name="product")
    op.drop_index(op.f("ix_product_fact_sku"), table_name="product")
    with op.batch_alter_table("product") as batch:
        batch.drop_column("sku_sync_error")
        batch.drop_column("sku_sync_status")
        batch.drop_column("planned_sku")
        batch.drop_column("fact_sku")

    op.drop_index("uq_product_sku_plan_active_planned_sku", table_name="product_sku_plan")
    op.drop_index("uq_product_sku_plan_active_product", table_name="product_sku_plan")
    op.drop_index("ix_product_sku_plan_planned_sku", table_name="product_sku_plan")
    op.drop_index("ix_product_sku_plan_product_id", table_name="product_sku_plan")
    op.drop_table("product_sku_plan")
