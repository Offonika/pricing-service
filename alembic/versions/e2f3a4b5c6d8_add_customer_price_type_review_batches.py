"""add customer price-type review batches

Revision ID: e2f3a4b5c6d8
Revises: d1a2b3c4e5f7
Create Date: 2026-07-31 18:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "e2f3a4b5c6d8"
down_revision = "d1a2b3c4e5f7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "customer_price_type_review_batch",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("batch_key", sa.String(length=128), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_files", sa.JSON(), nullable=False),
        sa.Column("expected_counts", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('ready','superseded')",
            name="ck_customer_price_type_review_batch_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_key", name="uq_customer_price_type_review_batch_key"),
    )
    op.create_index(
        "ix_customer_price_type_review_batch_status",
        "customer_price_type_review_batch",
        ["status"],
    )
    op.create_table(
        "customer_price_type_review_batch_item",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("batch_id", sa.Integer(), nullable=False),
        sa.Column("counterparty_ref", sa.String(length=64), nullable=False),
        sa.Column("counterparty_code", sa.String(length=64), nullable=False),
        sa.Column("expected_bucket", sa.String(length=32), nullable=False),
        sa.Column("expected_price_type", sa.String(length=255), nullable=True),
        sa.Column("source_name", sa.String(length=255), nullable=False),
        sa.Column("source_row", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "expected_bucket IN ('working_bronze','review_queue')",
            name="ck_customer_price_type_review_batch_item_bucket",
        ),
        sa.CheckConstraint(
            "counterparty_ref = lower(counterparty_ref)",
            name="ck_customer_price_type_review_batch_item_ref_lower",
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["customer_price_type_review_batch.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "batch_id",
            "counterparty_ref",
            name="uq_customer_price_type_review_batch_item_ref",
        ),
        sa.UniqueConstraint(
            "batch_id",
            "counterparty_code",
            name="uq_customer_price_type_review_batch_item_code",
        ),
    )
    op.create_index(
        "ix_customer_price_type_review_batch_item_bucket",
        "customer_price_type_review_batch_item",
        ["batch_id", "expected_bucket"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_customer_price_type_review_batch_item_bucket",
        table_name="customer_price_type_review_batch_item",
    )
    op.drop_table("customer_price_type_review_batch_item")
    op.drop_index(
        "ix_customer_price_type_review_batch_status",
        table_name="customer_price_type_review_batch",
    )
    op.drop_table("customer_price_type_review_batch")
