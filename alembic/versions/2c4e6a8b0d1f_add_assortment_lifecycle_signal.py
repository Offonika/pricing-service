"""add append-only assortment lifecycle signal store

Revision ID: 2c4e6a8b0d1f
Revises: 0a8c2e4f6b7d
Create Date: 2026-08-17 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "2c4e6a8b0d1f"
down_revision = "0a8c2e4f6b7d"
branch_labels = None
depends_on = None


JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _install_append_only_guard() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(sa.text("""
                CREATE FUNCTION reject_assortment_lifecycle_signal_mutation()
                RETURNS trigger
                LANGUAGE plpgsql
                AS $$
                BEGIN
                    RAISE EXCEPTION 'assortment_lifecycle_signal_is_append_only';
                END;
                $$
                """))
        op.execute(sa.text("""
                CREATE TRIGGER trg_assortment_lifecycle_signal_immutable
                BEFORE UPDATE OR DELETE ON assortment_lifecycle_signal
                FOR EACH ROW
                EXECUTE FUNCTION reject_assortment_lifecycle_signal_mutation()
                """))
    elif dialect == "sqlite":
        for operation in ("UPDATE", "DELETE"):
            suffix = operation.casefold()
            op.execute(sa.text(f"""
                    CREATE TRIGGER trg_assortment_lifecycle_signal_{suffix}_immutable
                    BEFORE {operation} ON assortment_lifecycle_signal
                    BEGIN
                        SELECT RAISE(
                            ABORT,
                            'assortment_lifecycle_signal_is_append_only'
                        );
                    END
                    """))


def _drop_append_only_guard() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            sa.text(
                "DROP TRIGGER trg_assortment_lifecycle_signal_immutable "
                "ON assortment_lifecycle_signal"
            )
        )
        op.execute(sa.text("DROP FUNCTION reject_assortment_lifecycle_signal_mutation()"))
    elif dialect == "sqlite":
        op.execute(sa.text("DROP TRIGGER trg_assortment_lifecycle_signal_update_immutable"))
        op.execute(sa.text("DROP TRIGGER trg_assortment_lifecycle_signal_delete_immutable"))


def upgrade() -> None:
    op.create_table(
        "assortment_lifecycle_signal",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("signal_key", sa.String(length=64), nullable=False),
        sa.Column("signal_type", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("source_event_id", sa.String(length=255), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("nomenclature_code", sa.String(length=64), nullable=True),
        sa.Column("display_family_key", sa.String(length=80), nullable=True),
        sa.Column("display_family_registry_version", sa.Integer(), nullable=True),
        sa.Column("reliability", sa.Numeric(precision=5, scale=4), nullable=False),
        sa.Column("reliability_reason", sa.String(length=255), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=28, scale=3), nullable=True),
        sa.Column("direction", sa.String(length=16), nullable=True),
        sa.Column("payload", JSON_TYPE, nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "reliability >= 0 AND reliability <= 1",
            name="ck_assortment_lifecycle_signal_reliability",
        ),
        sa.CheckConstraint(
            "schema_version = 'assortment_signal.v1'",
            name="ck_assortment_lifecycle_signal_schema_version",
        ),
        sa.CheckConstraint(
            "signal_type IN ('customer_sale', 'stock_availability', "
            "'supplier_order', 'supplier_receipt', 'cargo', 'kmp4', "
            "'site_order', 'site_cart', 'wordstat_direction')",
            name="ck_assortment_lifecycle_signal_type",
        ),
        sa.CheckConstraint(
            "available_at >= occurred_at",
            name="ck_assortment_lifecycle_signal_available_after_occurrence",
        ),
        sa.CheckConstraint(
            "nomenclature_code IS NOT NULL OR display_family_key IS NOT NULL",
            name="ck_assortment_lifecycle_signal_linkage",
        ),
        sa.CheckConstraint(
            "(display_family_key IS NULL AND display_family_registry_version IS NULL) "
            "OR (display_family_key IS NOT NULL "
            "AND display_family_registry_version IS NOT NULL)",
            name="ck_assortment_lifecycle_signal_family_version",
        ),
        sa.CheckConstraint(
            "display_family_registry_version IS NULL " "OR display_family_registry_version > 0",
            name="ck_assortment_lifecycle_signal_family_version_positive",
        ),
        sa.CheckConstraint(
            "quantity IS NULL OR quantity >= 0",
            name="ck_assortment_lifecycle_signal_quantity",
        ),
        sa.CheckConstraint(
            "direction IS NULL OR direction IN ('up', 'down', 'flat', 'unknown')",
            name="ck_assortment_lifecycle_signal_direction",
        ),
        sa.CheckConstraint(
            "signal_type <> 'wordstat_direction' "
            "OR (quantity IS NULL AND direction IS NOT NULL)",
            name="ck_assortment_lifecycle_signal_wordstat_direction_only",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "signal_key",
            name="uq_assortment_lifecycle_signal_key",
        ),
    )
    op.create_index(
        "ix_assortment_lifecycle_signal_available_type",
        "assortment_lifecycle_signal",
        ["available_at", "signal_type"],
    )
    op.create_index(
        "ix_assortment_lifecycle_signal_sku_available",
        "assortment_lifecycle_signal",
        ["nomenclature_code", "available_at"],
    )
    op.create_index(
        "ix_assortment_lifecycle_signal_family_available",
        "assortment_lifecycle_signal",
        ["display_family_key", "available_at"],
    )
    op.create_index(
        "ix_assortment_lifecycle_signal_source_event",
        "assortment_lifecycle_signal",
        ["source", "signal_type", "source_event_id"],
    )
    _install_append_only_guard()


def downgrade() -> None:
    _drop_append_only_guard()
    op.drop_index(
        "ix_assortment_lifecycle_signal_source_event",
        table_name="assortment_lifecycle_signal",
    )
    op.drop_index(
        "ix_assortment_lifecycle_signal_family_available",
        table_name="assortment_lifecycle_signal",
    )
    op.drop_index(
        "ix_assortment_lifecycle_signal_sku_available",
        table_name="assortment_lifecycle_signal",
    )
    op.drop_index(
        "ix_assortment_lifecycle_signal_available_type",
        table_name="assortment_lifecycle_signal",
    )
    op.drop_table("assortment_lifecycle_signal")
