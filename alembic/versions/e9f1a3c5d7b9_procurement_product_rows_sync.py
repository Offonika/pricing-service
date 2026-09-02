"""track procurement Smart Process product-row synchronization

Revision ID: e9f1a3c5d7b9
Revises: d8e0f2a4b6c8
Create Date: 2026-09-02 16:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "e9f1a3c5d7b9"
down_revision = "d8e0f2a4b6c8"
branch_labels = None
depends_on = None

TABLE_NAME = "procurement_order_formation"


def upgrade() -> None:
    op.add_column(
        TABLE_NAME,
        sa.Column("bitrix_product_rows_sync_state", sa.String(length=32), nullable=True),
    )
    op.add_column(
        TABLE_NAME,
        sa.Column("bitrix_product_rows_checksum", sa.String(length=64), nullable=True),
    )
    op.add_column(
        TABLE_NAME,
        sa.Column("bitrix_product_rows_expected_count", sa.Integer(), nullable=True),
    )
    op.add_column(
        TABLE_NAME,
        sa.Column("bitrix_product_rows_synced_count", sa.Integer(), nullable=True),
    )
    op.add_column(
        TABLE_NAME,
        sa.Column("bitrix_product_rows_synced_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        TABLE_NAME,
        sa.Column("bitrix_product_rows_error", sa.Text(), nullable=True),
    )
    op.execute(sa.text("""
            UPDATE procurement_order_formation
            SET bitrix_product_rows_sync_state = CASE
                WHEN bitrix_entity_type_id = 1056 AND bitrix_item_id IS NOT NULL THEN 'pending'
                ELSE 'not_applicable'
            END
            """))


def downgrade() -> None:
    op.drop_column(TABLE_NAME, "bitrix_product_rows_error")
    op.drop_column(TABLE_NAME, "bitrix_product_rows_synced_at")
    op.drop_column(TABLE_NAME, "bitrix_product_rows_synced_count")
    op.drop_column(TABLE_NAME, "bitrix_product_rows_expected_count")
    op.drop_column(TABLE_NAME, "bitrix_product_rows_checksum")
    op.drop_column(TABLE_NAME, "bitrix_product_rows_sync_state")
