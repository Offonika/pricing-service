"""Add idempotent Bitrix product-card sync state.

Revision ID: c6d7e8f90123
Revises: b5d6e7f80920
Create Date: 2026-09-02
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "c6d7e8f90123"
down_revision = "b5d6e7f80920"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "procurement_product_card_sync_state",
        sa.Column("product_xml_id", sa.String(length=64), nullable=False),
        sa.Column("bitrix_product_id", sa.String(length=64), nullable=True),
        sa.Column("scope", sa.String(length=32), nullable=False, server_default="displays"),
        sa.Column("snapshot_hash", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("desired_fields", sa.JSON(), nullable=False),
        sa.Column("readback_fields", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("last_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("last_success_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("product_xml_id", name="uq_proc_product_card_sync_xml_id"),
    )
    op.create_index(
        "ix_proc_product_card_sync_bitrix_product",
        "procurement_product_card_sync_state",
        ["bitrix_product_id"],
        unique=False,
    )
    op.create_index(
        "ix_proc_product_card_sync_status",
        "procurement_product_card_sync_state",
        ["status", "updated_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_proc_product_card_sync_status",
        table_name="procurement_product_card_sync_state",
    )
    op.drop_index(
        "ix_proc_product_card_sync_bitrix_product",
        table_name="procurement_product_card_sync_state",
    )
    op.drop_table("procurement_product_card_sync_state")
