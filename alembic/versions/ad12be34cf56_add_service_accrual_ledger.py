"""add recurring service accrual ledger

Revision ID: ad12be34cf56
Revises: 9c0d1e2f3a45
Create Date: 2026-07-12 21:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "ad12be34cf56"
down_revision = "9c0d1e2f3a45"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "executive_service_accrual_rule",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("rule_key", sa.String(length=120), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("counterparty_ref", sa.String(length=40), nullable=False),
        sa.Column("counterparty_name", sa.String(length=255), nullable=False),
        sa.Column("contract_ref", sa.String(length=40), nullable=False),
        sa.Column("contract_name", sa.String(length=255), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("expense_line_key", sa.String(length=96), nullable=False),
        sa.Column("expense_line_label", sa.String(length=255), nullable=False),
        sa.Column("monthly_amount_rub", sa.Numeric(18, 2), nullable=False),
        sa.Column("recognition_day", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("approved_by", sa.String(length=160), nullable=False),
        sa.Column("approval_note", sa.String(length=1000), nullable=True),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("rule_key", "version", name="uq_service_accrual_rule_version"),
    )
    op.create_index(
        "ix_service_accrual_rule_contract_active",
        "executive_service_accrual_rule",
        ["contract_ref", "active"],
    )
    op.create_table(
        "executive_service_accrual_entry",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("rule_id", sa.Integer(), nullable=False),
        sa.Column("period_month", sa.Date(), nullable=False),
        sa.Column("recognition_date", sa.Date(), nullable=False),
        sa.Column("counterparty_ref", sa.String(length=40), nullable=False),
        sa.Column("counterparty_name", sa.String(length=255), nullable=False),
        sa.Column("contract_ref", sa.String(length=40), nullable=False),
        sa.Column("contract_name", sa.String(length=255), nullable=False),
        sa.Column("expense_line_key", sa.String(length=96), nullable=False),
        sa.Column("expense_line_label", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("recognition_method", sa.String(length=48), nullable=False),
        sa.Column("recognized_amount_rub", sa.Numeric(18, 2), nullable=False),
        sa.Column("payment_amount_rub", sa.Numeric(18, 2), nullable=False),
        sa.Column("cashflow_expense_replaced_rub", sa.Numeric(18, 2), nullable=False),
        sa.Column("source_document_ref", sa.String(length=80), nullable=True),
        sa.Column("source_status", sa.String(length=32), nullable=False),
        sa.Column("source_as_of", sa.Date(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("generated_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["rule_id"], ["executive_service_accrual_rule.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("rule_id", "period_month", name="uq_service_accrual_rule_month"),
    )
    op.create_index(
        "ix_service_accrual_entry_period_status",
        "executive_service_accrual_entry",
        ["period_month", "status"],
    )
    op.create_index(
        "ix_service_accrual_entry_counterparty",
        "executive_service_accrual_entry",
        ["counterparty_ref", "period_month"],
    )
    op.create_table(
        "executive_service_accrual_audit",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("entry_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=48), nullable=False),
        sa.Column("actor", sa.String(length=160), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["entry_id"], ["executive_service_accrual_entry.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_service_accrual_audit_entry",
        "executive_service_accrual_audit",
        ["entry_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_service_accrual_audit_entry", table_name="executive_service_accrual_audit")
    op.drop_table("executive_service_accrual_audit")
    op.drop_index(
        "ix_service_accrual_entry_counterparty", table_name="executive_service_accrual_entry"
    )
    op.drop_index(
        "ix_service_accrual_entry_period_status", table_name="executive_service_accrual_entry"
    )
    op.drop_table("executive_service_accrual_entry")
    op.drop_index(
        "ix_service_accrual_rule_contract_active", table_name="executive_service_accrual_rule"
    )
    op.drop_table("executive_service_accrual_rule")
