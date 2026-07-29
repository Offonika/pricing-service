"""add customer settlement snapshot and scoped access

Revision ID: c3d4e5f6a7b9
Revises: e2b3c4d5e6f8
Create Date: 2026-07-29 22:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "c3d4e5f6a7b9"
down_revision = "e2b3c4d5e6f8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "customer_settlement_revision",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("status", sa.String(length=16), server_default="loading", nullable=False),
        sa.Column("organization_ref", sa.String(length=64), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_db_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_mode", sa.String(length=96), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("expected_row_count", sa.Integer(), nullable=False),
        sa.Column("loaded_row_count", sa.Integer(), nullable=False),
        sa.Column("zero_row_count", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=96), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('loading','active','superseded','failed')",
            name="ck_customer_settlement_revision_status",
        ),
        sa.CheckConstraint(
            "currency = 'RUB'",
            name="ck_customer_settlement_revision_currency",
        ),
        sa.CheckConstraint(
            "expected_row_count >= 0 AND loaded_row_count >= 0 AND zero_row_count >= 0",
            name="ck_customer_settlement_revision_nonnegative_counts",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_hash"),
    )
    op.create_index(
        "uq_customer_settlement_revision_active",
        "customer_settlement_revision",
        ["status"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
        sqlite_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ix_customer_settlement_revision_status_created",
        "customer_settlement_revision",
        ["status", "created_at"],
    )
    op.create_table(
        "customer_settlement_balance",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("revision_id", sa.Integer(), nullable=False),
        sa.Column("counterparty_ref", sa.String(length=64), nullable=False),
        sa.Column("signed_balance", sa.Numeric(18, 2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "currency = 'RUB'",
            name="ck_customer_settlement_balance_currency",
        ),
        sa.ForeignKeyConstraint(
            ["revision_id"],
            ["customer_settlement_revision.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "revision_id",
            "counterparty_ref",
            name="uq_customer_settlement_balance_revision_counterparty",
        ),
    )
    op.create_index(
        "ix_customer_settlement_balance_counterparty_revision",
        "customer_settlement_balance",
        ["counterparty_ref", "revision_id"],
    )
    op.create_table(
        "customer_settlement_mapping_revision",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("status", sa.String(length=16), server_default="loading", nullable=False),
        sa.Column("source_name", sa.String(length=64), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("source_checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expected_entry_count", sa.Integer(), nullable=False),
        sa.Column("loaded_entry_count", sa.Integer(), nullable=False),
        sa.Column("ambiguous_count", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=96), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('loading','active','superseded','failed')",
            name="ck_customer_settlement_mapping_revision_status",
        ),
        sa.CheckConstraint(
            "expected_entry_count >= 0 AND loaded_entry_count >= 0 AND ambiguous_count >= 0",
            name="ck_customer_settlement_mapping_revision_nonnegative_counts",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_hash"),
    )
    op.create_index(
        "uq_customer_settlement_mapping_revision_active",
        "customer_settlement_mapping_revision",
        ["status"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
        sqlite_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ix_customer_settlement_mapping_revision_status_created",
        "customer_settlement_mapping_revision",
        ["status", "created_at"],
    )
    op.create_table(
        "customer_settlement_mapping_entry",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("revision_id", sa.Integer(), nullable=False),
        sa.Column("site_user_id", sa.String(length=32), nullable=False),
        sa.Column("cluster_id", sa.String(length=128), nullable=True),
        sa.Column("counterparty_ref", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('linked','not_linked','ambiguous')",
            name="ck_customer_settlement_mapping_entry_status",
        ),
        sa.ForeignKeyConstraint(
            ["revision_id"],
            ["customer_settlement_mapping_revision.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "revision_id",
            "site_user_id",
            name="uq_customer_settlement_mapping_entry_revision_user",
        ),
    )
    op.create_index(
        "ix_customer_settlement_mapping_entry_user_revision",
        "customer_settlement_mapping_entry",
        ["site_user_id", "revision_id"],
    )
    op.create_table(
        "customer_settlement_pilot_access",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("site_user_id", sa.String(length=32), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("site_user_id", name="uq_customer_settlement_pilot_user"),
    )
    op.create_index(
        "ix_customer_settlement_pilot_enabled",
        "customer_settlement_pilot_access",
        ["enabled"],
    )
    op.create_table(
        "customer_settlement_assertion_jti",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("jti_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("jti_hash", name="uq_customer_settlement_assertion_jti_hash"),
    )
    op.create_index(
        "ix_customer_settlement_assertion_expires",
        "customer_settlement_assertion_jti",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_customer_settlement_assertion_expires",
        table_name="customer_settlement_assertion_jti",
    )
    op.drop_table("customer_settlement_assertion_jti")
    op.drop_index(
        "ix_customer_settlement_pilot_enabled",
        table_name="customer_settlement_pilot_access",
    )
    op.drop_table("customer_settlement_pilot_access")
    op.drop_index(
        "ix_customer_settlement_mapping_entry_user_revision",
        table_name="customer_settlement_mapping_entry",
    )
    op.drop_table("customer_settlement_mapping_entry")
    op.drop_index(
        "ix_customer_settlement_mapping_revision_status_created",
        table_name="customer_settlement_mapping_revision",
    )
    op.drop_index(
        "uq_customer_settlement_mapping_revision_active",
        table_name="customer_settlement_mapping_revision",
    )
    op.drop_table("customer_settlement_mapping_revision")
    op.drop_index(
        "ix_customer_settlement_balance_counterparty_revision",
        table_name="customer_settlement_balance",
    )
    op.drop_table("customer_settlement_balance")
    op.drop_index(
        "ix_customer_settlement_revision_status_created",
        table_name="customer_settlement_revision",
    )
    op.drop_index(
        "uq_customer_settlement_revision_active",
        table_name="customer_settlement_revision",
    )
    op.drop_table("customer_settlement_revision")
