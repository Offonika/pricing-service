"""add durable customer accounts and GUID settlement mappings

Revision ID: d9e1f3a5b7c9
Revises: c3d4e5f6a7b9
Create Date: 2026-08-11 10:15:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "d9e1f3a5b7c9"
down_revision = "c3d4e5f6a7b9"
branch_labels = None
depends_on = None


def _guid_from_ref_sql(column: str, *, dialect: str) -> str:
    if dialect == "postgresql":
        return (
            f"lower(substr({column}, 27, 8) || '-' || substr({column}, 23, 4) || '-' || "
            f"substr({column}, 19, 4) || '-' || substr({column}, 3, 4) || '-' || "
            f"substr({column}, 7, 12))"
        )
    return (
        f"lower(substr({column}, 27, 8) || '-' || substr({column}, 23, 4) || '-' || "
        f"substr({column}, 19, 4) || '-' || substr({column}, 3, 4) || '-' || "
        f"substr({column}, 7, 12))"
    )


def upgrade() -> None:
    op.create_table(
        "customer_account",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('active','blocked')",
            name="ck_customer_account_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_customer_account_status", "customer_account", ["status"])

    op.create_table(
        "customer_account_site_binding",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("customer_account_id", sa.Integer(), nullable=False),
        sa.Column(
            "site_code", sa.String(length=64), server_default="master-mobile.ru", nullable=False
        ),
        sa.Column("site_user_id", sa.String(length=32), nullable=False),
        sa.Column("cluster_id", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("mapping_revision_id", sa.Integer(), nullable=True),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('active','revoked')",
            name="ck_customer_account_site_binding_status",
        ),
        sa.ForeignKeyConstraint(
            ["customer_account_id"], ["customer_account.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["mapping_revision_id"],
            ["customer_settlement_mapping_revision.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_customer_account_site_binding_active_user",
        "customer_account_site_binding",
        ["site_code", "site_user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
        sqlite_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ix_customer_account_site_binding_account_status",
        "customer_account_site_binding",
        ["customer_account_id", "status"],
    )

    op.create_table(
        "customer_account_source_binding",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("customer_account_id", sa.Integer(), nullable=False),
        sa.Column("source_system", sa.String(length=32), nullable=False),
        sa.Column("counterparty_guid", sa.String(length=36), nullable=False),
        sa.Column("counterparty_ref", sa.String(length=64), nullable=False),
        sa.Column("organization_guid", sa.String(length=36), nullable=False),
        sa.Column("organization_ref", sa.String(length=64), nullable=False),
        sa.Column("counterparty_code", sa.String(length=64), nullable=True),
        sa.Column("identity_control_hash", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("mapping_revision_id", sa.Integer(), nullable=True),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('active','revoked')",
            name="ck_customer_account_source_binding_status",
        ),
        sa.CheckConstraint(
            "counterparty_guid = lower(counterparty_guid) AND length(counterparty_guid) = 36",
            name="ck_customer_account_source_binding_guid",
        ),
        sa.CheckConstraint(
            "organization_guid = lower(organization_guid) AND length(organization_guid) = 36",
            name="ck_customer_account_source_binding_org_guid",
        ),
        sa.ForeignKeyConstraint(
            ["customer_account_id"], ["customer_account.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["mapping_revision_id"],
            ["customer_settlement_mapping_revision.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_customer_account_source_binding_active_account",
        "customer_account_source_binding",
        ["customer_account_id", "source_system", "organization_guid"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
        sqlite_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "uq_customer_account_source_binding_active_identity",
        "customer_account_source_binding",
        ["source_system", "counterparty_guid", "organization_guid"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
        sqlite_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ix_customer_account_source_binding_account_status",
        "customer_account_source_binding",
        ["customer_account_id", "status"],
    )

    op.add_column(
        "customer_settlement_revision",
        sa.Column("organization_guid", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "customer_settlement_balance",
        sa.Column("counterparty_guid", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "customer_settlement_mapping_entry",
        sa.Column("counterparty_guid", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "customer_settlement_mapping_entry",
        sa.Column("source_system", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "customer_settlement_mapping_entry",
        sa.Column("organization_guid", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "customer_settlement_mapping_entry",
        sa.Column("customer_account_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "customer_settlement_mapping_entry",
        sa.Column("source_binding_id", sa.Integer(), nullable=True),
    )

    dialect = op.get_bind().dialect.name
    op.execute(
        sa.text(
            "UPDATE customer_settlement_revision "
            f"SET organization_guid = {_guid_from_ref_sql('organization_ref', dialect=dialect)} "
            "WHERE organization_guid IS NULL"
        )
    )
    op.execute(
        sa.text(
            "UPDATE customer_settlement_balance "
            f"SET counterparty_guid = {_guid_from_ref_sql('counterparty_ref', dialect=dialect)} "
            "WHERE counterparty_guid IS NULL"
        )
    )
    op.execute(
        sa.text(
            "UPDATE customer_settlement_mapping_entry "
            f"SET counterparty_guid = {_guid_from_ref_sql('counterparty_ref', dialect=dialect)}, "
            "source_system = 'ut103', "
            "organization_guid = '8227ea80-1112-11e2-b34a-0025901e48ef' "
            "WHERE status = 'linked' AND counterparty_ref IS NOT NULL"
        )
    )

    if dialect == "sqlite":
        with op.batch_alter_table("customer_settlement_balance") as batch:
            batch.alter_column("counterparty_guid", existing_type=sa.String(36), nullable=False)
            batch.create_unique_constraint(
                "uq_customer_settlement_balance_revision_guid",
                ["revision_id", "counterparty_guid"],
            )
        with op.batch_alter_table("customer_settlement_mapping_entry") as batch:
            batch.create_foreign_key(
                "fk_customer_settlement_mapping_entry_account",
                "customer_account",
                ["customer_account_id"],
                ["id"],
                ondelete="RESTRICT",
            )
            batch.create_foreign_key(
                "fk_customer_settlement_mapping_entry_source_binding",
                "customer_account_source_binding",
                ["source_binding_id"],
                ["id"],
                ondelete="RESTRICT",
            )
    else:
        op.alter_column(
            "customer_settlement_balance",
            "counterparty_guid",
            existing_type=sa.String(length=36),
            nullable=False,
        )
        op.create_unique_constraint(
            "uq_customer_settlement_balance_revision_guid",
            "customer_settlement_balance",
            ["revision_id", "counterparty_guid"],
        )
        op.create_foreign_key(
            "fk_customer_settlement_mapping_entry_account",
            "customer_settlement_mapping_entry",
            "customer_account",
            ["customer_account_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        op.create_foreign_key(
            "fk_customer_settlement_mapping_entry_source_binding",
            "customer_settlement_mapping_entry",
            "customer_account_source_binding",
            ["source_binding_id"],
            ["id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        with op.batch_alter_table("customer_settlement_mapping_entry") as batch:
            batch.drop_constraint(
                "fk_customer_settlement_mapping_entry_source_binding",
                type_="foreignkey",
            )
            batch.drop_constraint(
                "fk_customer_settlement_mapping_entry_account",
                type_="foreignkey",
            )
            batch.drop_column("source_binding_id")
            batch.drop_column("customer_account_id")
            batch.drop_column("organization_guid")
            batch.drop_column("source_system")
            batch.drop_column("counterparty_guid")
        with op.batch_alter_table("customer_settlement_balance") as batch:
            batch.drop_constraint(
                "uq_customer_settlement_balance_revision_guid",
                type_="unique",
            )
            batch.drop_column("counterparty_guid")
        with op.batch_alter_table("customer_settlement_revision") as batch:
            batch.drop_column("organization_guid")
    else:
        op.drop_constraint(
            "fk_customer_settlement_mapping_entry_source_binding",
            "customer_settlement_mapping_entry",
            type_="foreignkey",
        )
        op.drop_constraint(
            "fk_customer_settlement_mapping_entry_account",
            "customer_settlement_mapping_entry",
            type_="foreignkey",
        )
        op.drop_constraint(
            "uq_customer_settlement_balance_revision_guid",
            "customer_settlement_balance",
            type_="unique",
        )
        op.drop_column("customer_settlement_mapping_entry", "source_binding_id")
        op.drop_column("customer_settlement_mapping_entry", "customer_account_id")
        op.drop_column("customer_settlement_mapping_entry", "organization_guid")
        op.drop_column("customer_settlement_mapping_entry", "source_system")
        op.drop_column("customer_settlement_mapping_entry", "counterparty_guid")
        op.drop_column("customer_settlement_balance", "counterparty_guid")
        op.drop_column("customer_settlement_revision", "organization_guid")

    op.drop_index(
        "ix_customer_account_source_binding_account_status",
        table_name="customer_account_source_binding",
    )
    op.drop_index(
        "uq_customer_account_source_binding_active_identity",
        table_name="customer_account_source_binding",
    )
    op.drop_index(
        "uq_customer_account_source_binding_active_account",
        table_name="customer_account_source_binding",
    )
    op.drop_table("customer_account_source_binding")
    op.drop_index(
        "ix_customer_account_site_binding_account_status",
        table_name="customer_account_site_binding",
    )
    op.drop_index(
        "uq_customer_account_site_binding_active_user",
        table_name="customer_account_site_binding",
    )
    op.drop_table("customer_account_site_binding")
    op.drop_index("ix_customer_account_status", table_name="customer_account")
    op.drop_table("customer_account")
