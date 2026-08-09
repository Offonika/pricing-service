"""add procurement supplier profiles

Revision ID: f3a4b5c6d7e9
Revises: e2f3a4b5c6d8
Create Date: 2026-08-01 23:15:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "f3a4b5c6d7e9"
down_revision = "e2f3a4b5c6d8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "procurement_classification_proposal",
        sa.Column("rejected_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "procurement_classification_proposal",
        sa.Column("rejected_by_actor", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "procurement_classification_proposal",
        sa.Column("rejected_by_bitrix_user_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "procurement_classification_proposal",
        sa.Column("rejected_by_name", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "procurement_classification_proposal",
        sa.Column("rejection_reason", sa.Text(), nullable=True),
    )
    op.create_table(
        "procurement_supplier_profile",
        sa.Column("supplier_ref", sa.String(length=64), nullable=False),
        sa.Column("supplier_code", sa.String(length=64), nullable=True),
        sa.Column("supplier_name", sa.String(length=500), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("qualification_class", sa.String(length=1), nullable=True),
        sa.Column("qualification_label", sa.String(length=255), nullable=True),
        sa.Column("advantages", sa.JSON(), nullable=False),
        sa.Column("internal_note", sa.Text(), nullable=True),
        sa.Column("payment_terms", sa.String(length=500), nullable=True),
        sa.Column("credit_days", sa.Integer(), nullable=True),
        sa.Column("credit_limit", sa.Numeric(18, 2), nullable=True),
        sa.Column("terms_source", sa.String(length=64), nullable=False),
        sa.Column("terms_status", sa.String(length=32), nullable=False),
        sa.Column("history_order_count", sa.Integer(), nullable=True),
        sa.Column("supplier_prepare_days", sa.Integer(), nullable=True),
        sa.Column("logistics_days", sa.Integer(), nullable=True),
        sa.Column("lead_time_days", sa.Integer(), nullable=True),
        sa.Column("lead_time_confidence", sa.String(length=32), nullable=True),
        sa.Column("price_history_count", sa.Integer(), nullable=True),
        sa.Column("supplier_defect_pct", sa.Numeric(7, 3), nullable=True),
        sa.Column("supplier_defect_history_units", sa.Integer(), nullable=True),
        sa.Column("supplier_defect_confidence", sa.String(length=32), nullable=True),
        sa.Column("facts_payload", sa.JSON(), nullable=False),
        sa.Column("facts_updated_at", sa.DateTime(), nullable=True),
        sa.Column("manual_updated_by_actor", sa.String(length=255), nullable=True),
        sa.Column("manual_updated_by_bitrix_user_id", sa.String(length=64), nullable=True),
        sa.Column("manual_updated_by_name", sa.String(length=255), nullable=True),
        sa.Column("manual_updated_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.CheckConstraint(
            "qualification_class IS NULL OR qualification_class IN ('A','B','C')",
            name="ck_proc_supplier_profile_class",
        ),
        sa.CheckConstraint(
            "terms_status IN ('ready','partial','missing')",
            name="ck_proc_supplier_profile_terms_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("supplier_ref", name="uq_proc_supplier_profile_ref"),
    )
    op.create_index(
        "ix_proc_supplier_profile_code",
        "procurement_supplier_profile",
        ["supplier_code"],
    )
    op.create_index(
        "ix_proc_supplier_profile_class",
        "procurement_supplier_profile",
        ["qualification_class"],
    )


def downgrade() -> None:
    op.drop_index("ix_proc_supplier_profile_class", table_name="procurement_supplier_profile")
    op.drop_index("ix_proc_supplier_profile_code", table_name="procurement_supplier_profile")
    op.drop_table("procurement_supplier_profile")
    op.drop_column("procurement_classification_proposal", "rejection_reason")
    op.drop_column("procurement_classification_proposal", "rejected_by_name")
    op.drop_column("procurement_classification_proposal", "rejected_by_bitrix_user_id")
    op.drop_column("procurement_classification_proposal", "rejected_by_actor")
    op.drop_column("procurement_classification_proposal", "rejected_at")
