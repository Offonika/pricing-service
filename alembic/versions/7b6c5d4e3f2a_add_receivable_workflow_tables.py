"""add receivable workflow tables

Revision ID: 7b6c5d4e3f2a
Revises: 4e2a9c8d7f10
Create Date: 2026-05-07 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7b6c5d4e3f2a"
down_revision: str | Sequence[str] | None = "4e2a9c8d7f10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "receivable_work_item",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("stable_key", sa.String(length=160), nullable=False),
        sa.Column("counterparty_ref", sa.String(length=64), nullable=False),
        sa.Column("counterparty_name", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("current_debt_key", sa.String(length=220), nullable=True),
        sa.Column("current_balance", sa.Numeric(18, 2), nullable=False),
        sa.Column("origin_document_ref", sa.String(length=64), nullable=True),
        sa.Column("origin_document_number", sa.String(length=64), nullable=True),
        sa.Column("origin_document_date", sa.DateTime(), nullable=True),
        sa.Column("due_date", sa.DateTime(), nullable=True),
        sa.Column("overdue_days", sa.Integer(), nullable=True),
        sa.Column("age_days", sa.Integer(), nullable=True),
        sa.Column("origin_manager_ref", sa.String(length=64), nullable=True),
        sa.Column("origin_manager_name", sa.String(length=255), nullable=True),
        sa.Column("current_manager_ref", sa.String(length=64), nullable=True),
        sa.Column("current_manager_name", sa.String(length=255), nullable=True),
        sa.Column("department_ref", sa.String(length=64), nullable=True),
        sa.Column("department_name", sa.String(length=255), nullable=True),
        sa.Column("phone", sa.String(length=64), nullable=True),
        sa.Column("phone_status", sa.String(length=32), nullable=False),
        sa.Column("bitrix_item_id", sa.Integer(), nullable=True),
        sa.Column("bitrix_stage_id", sa.String(length=128), nullable=True),
        sa.Column("bitrix_last_sync_at", sa.DateTime(), nullable=True),
        sa.Column("bitrix_last_error", sa.Text(), nullable=True),
        sa.Column("bitrix_detail_url", sa.String(length=512), nullable=True),
        sa.Column("assigned_bitrix_user_id", sa.Integer(), nullable=True),
        sa.Column("assigned_source", sa.String(length=64), nullable=True),
        sa.Column(
            "needs_call_today",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("last_sms_at", sa.DateTime(), nullable=True),
        sa.Column("last_sms_status", sa.String(length=32), nullable=True),
        sa.Column("last_sms_error", sa.Text(), nullable=True),
        sa.Column("last_contact_comment", sa.Text(), nullable=True),
        sa.Column("promised_payment_date", sa.DateTime(), nullable=True),
        sa.Column("next_action_date", sa.DateTime(), nullable=True),
        sa.Column("last_manager_update_at", sa.DateTime(), nullable=True),
        sa.Column("escalated_at", sa.DateTime(), nullable=True),
        sa.Column("escalation_level", sa.String(length=64), nullable=True),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
        sa.Column("chain_documents", sa.JSON(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("stable_key", name="uq_receivable_work_item_stable_key"),
    )
    op.create_index(
        "ix_receivable_work_item_counterparty_ref",
        "receivable_work_item",
        ["counterparty_ref"],
    )
    op.create_index(
        "ix_receivable_work_item_current_debt_key",
        "receivable_work_item",
        ["current_debt_key"],
    )
    op.create_index(
        "ix_receivable_work_item_status",
        "receivable_work_item",
        ["status"],
    )
    op.create_index(
        "ix_receivable_work_item_bitrix_item_id",
        "receivable_work_item",
        ["bitrix_item_id"],
    )

    op.create_table(
        "receivable_work_event",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("work_item_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("event_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(
            ["work_item_id"],
            ["receivable_work_item.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_receivable_work_event_idempotency_key",
        ),
    )
    op.create_index(
        "ix_receivable_work_event_work_item_id",
        "receivable_work_event",
        ["work_item_id"],
    )
    op.create_index(
        "ix_receivable_work_event_event_type",
        "receivable_work_event",
        ["event_type"],
    )
    op.create_index(
        "ix_receivable_work_event_event_at",
        "receivable_work_event",
        ["event_at"],
    )

    op.create_table(
        "receivable_sms_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("work_item_id", sa.Integer(), nullable=True),
        sa.Column("stable_key", sa.String(length=160), nullable=False),
        sa.Column("counterparty_ref", sa.String(length=64), nullable=False),
        sa.Column("debt_key", sa.String(length=220), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("phone", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("message_text", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["work_item_id"],
            ["receivable_work_item.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("debt_key", "business_date", name="uq_receivable_sms_log_debt_date"),
    )
    op.create_index("ix_receivable_sms_log_work_item_id", "receivable_sms_log", ["work_item_id"])
    op.create_index("ix_receivable_sms_log_stable_key", "receivable_sms_log", ["stable_key"])
    op.create_index(
        "ix_receivable_sms_log_counterparty_ref",
        "receivable_sms_log",
        ["counterparty_ref"],
    )
    op.create_index("ix_receivable_sms_log_debt_key", "receivable_sms_log", ["debt_key"])
    op.create_index("ix_receivable_sms_log_status", "receivable_sms_log", ["status"])


def downgrade() -> None:
    op.drop_index("ix_receivable_sms_log_status", table_name="receivable_sms_log")
    op.drop_index("ix_receivable_sms_log_debt_key", table_name="receivable_sms_log")
    op.drop_index("ix_receivable_sms_log_counterparty_ref", table_name="receivable_sms_log")
    op.drop_index("ix_receivable_sms_log_stable_key", table_name="receivable_sms_log")
    op.drop_index("ix_receivable_sms_log_work_item_id", table_name="receivable_sms_log")
    op.drop_table("receivable_sms_log")

    op.drop_index("ix_receivable_work_event_event_at", table_name="receivable_work_event")
    op.drop_index("ix_receivable_work_event_event_type", table_name="receivable_work_event")
    op.drop_index("ix_receivable_work_event_work_item_id", table_name="receivable_work_event")
    op.drop_table("receivable_work_event")

    op.drop_index("ix_receivable_work_item_bitrix_item_id", table_name="receivable_work_item")
    op.drop_index("ix_receivable_work_item_status", table_name="receivable_work_item")
    op.drop_index("ix_receivable_work_item_current_debt_key", table_name="receivable_work_item")
    op.drop_index("ix_receivable_work_item_counterparty_ref", table_name="receivable_work_item")
    op.drop_table("receivable_work_item")
