"""add card balance reconciliation

Revision ID: cb01d2e3f4a5
Revises: b1c2d3e4f5a6, 3d4e5f6a7b8
Create Date: 2026-04-23 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "cb01d2e3f4a5"
down_revision: str | Sequence[str] | None = ("b1c2d3e4f5a6", "3d4e5f6a7b8")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "card_balance_cashbox",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("onec_cashbox_ref_hex", sa.String(length=64), nullable=True),
        sa.Column("onec_cashbox_code", sa.String(length=64), nullable=False),
        sa.Column("onec_cashbox_name", sa.String(length=255), nullable=False),
        sa.Column("currency_code", sa.String(length=16), nullable=True),
        sa.Column("currency_name", sa.String(length=64), nullable=True),
        sa.Column("card_last4", sa.String(length=4), nullable=True),
        sa.Column("store_name", sa.String(length=255), nullable=True),
        sa.Column("employee_last_name", sa.String(length=255), nullable=True),
        sa.Column("employee_id", sa.String(length=64), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("needs_manual_review", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("review_reason", sa.String(length=1000), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("onec_cashbox_code", name="uq_card_balance_cashbox_onec_code"),
    )
    op.create_index("ix_card_balance_cashbox_card_last4", "card_balance_cashbox", ["card_last4"])
    op.create_index(
        "ix_card_balance_cashbox_employee_last_name",
        "card_balance_cashbox",
        ["employee_last_name"],
    )
    op.create_index("ix_card_balance_cashbox_is_active", "card_balance_cashbox", ["is_active"])

    op.create_table(
        "card_balance_reconciliation",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("cashbox_id", sa.Integer(), nullable=True),
        sa.Column("employee_id", sa.String(length=64), nullable=True),
        sa.Column("employee_name", sa.String(length=255), nullable=True),
        sa.Column("employee_last_name", sa.String(length=255), nullable=True),
        sa.Column("card_last4", sa.String(length=4), nullable=True),
        sa.Column("onec_cashbox_code", sa.String(length=64), nullable=True),
        sa.Column("onec_cashbox_name", sa.String(length=255), nullable=True),
        sa.Column("source_channel", sa.String(length=32), server_default="bitrix", nullable=False),
        sa.Column("bitrix_item_id", sa.String(length=64), nullable=True),
        sa.Column("bitrix_stage_id", sa.String(length=128), nullable=True),
        sa.Column("screenshot_file_id", sa.String(length=255), nullable=True),
        sa.Column("submitted_at", sa.DateTime(), nullable=True),
        sa.Column("screenshot_taken_at", sa.DateTime(), nullable=True),
        sa.Column("manual_balance", sa.Numeric(18, 2), nullable=True),
        sa.Column("recognized_balance", sa.Numeric(18, 2), nullable=True),
        sa.Column("recognition_confidence", sa.Numeric(5, 4), nullable=True),
        sa.Column("onec_balance_at", sa.DateTime(), nullable=True),
        sa.Column("onec_balance", sa.Numeric(18, 2), nullable=True),
        sa.Column("diff_amount", sa.Numeric(18, 2), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("reviewer_id", sa.String(length=64), nullable=True),
        sa.Column("resolution_comment", sa.String(length=1000), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("due_at", sa.DateTime(), nullable=True),
        sa.Column("bitrix_last_sync_at", sa.DateTime(), nullable=True),
        sa.Column("bitrix_last_error", sa.String(length=1000), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["cashbox_id"], ["card_balance_cashbox.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("external_id", name="uq_card_balance_reconciliation_external_id"),
        sa.UniqueConstraint("bitrix_item_id", name="uq_card_balance_reconciliation_bitrix_item_id"),
    )
    op.create_index(
        "ix_card_balance_reconciliation_business_date",
        "card_balance_reconciliation",
        ["business_date"],
    )
    op.create_index(
        "ix_card_balance_reconciliation_status",
        "card_balance_reconciliation",
        ["status"],
    )
    op.create_index(
        "ix_card_balance_reconciliation_cashbox_date",
        "card_balance_reconciliation",
        ["cashbox_id", "business_date"],
    )
    op.create_index(
        "ix_card_balance_reconciliation_due_at",
        "card_balance_reconciliation",
        ["due_at"],
    )

    op.create_table(
        "card_balance_reconciliation_event",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("reconciliation_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("event_at", sa.DateTime(), nullable=False),
        sa.Column("actor_external_id", sa.String(length=64), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("comment", sa.String(length=1000), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["reconciliation_id"],
            ["card_balance_reconciliation.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_card_balance_event_idempotency_key"),
    )
    op.create_index(
        "ix_card_balance_event_reconciliation_at",
        "card_balance_reconciliation_event",
        ["reconciliation_id", "event_at"],
    )
    op.create_index(
        "ix_card_balance_event_type",
        "card_balance_reconciliation_event",
        ["event_type"],
    )


def downgrade() -> None:
    op.drop_index("ix_card_balance_event_type", table_name="card_balance_reconciliation_event")
    op.drop_index(
        "ix_card_balance_event_reconciliation_at",
        table_name="card_balance_reconciliation_event",
    )
    op.drop_table("card_balance_reconciliation_event")
    op.drop_index("ix_card_balance_reconciliation_due_at", table_name="card_balance_reconciliation")
    op.drop_index(
        "ix_card_balance_reconciliation_cashbox_date",
        table_name="card_balance_reconciliation",
    )
    op.drop_index("ix_card_balance_reconciliation_status", table_name="card_balance_reconciliation")
    op.drop_index(
        "ix_card_balance_reconciliation_business_date",
        table_name="card_balance_reconciliation",
    )
    op.drop_table("card_balance_reconciliation")
    op.drop_index("ix_card_balance_cashbox_is_active", table_name="card_balance_cashbox")
    op.drop_index("ix_card_balance_cashbox_employee_last_name", table_name="card_balance_cashbox")
    op.drop_index("ix_card_balance_cashbox_card_last4", table_name="card_balance_cashbox")
    op.drop_table("card_balance_cashbox")
