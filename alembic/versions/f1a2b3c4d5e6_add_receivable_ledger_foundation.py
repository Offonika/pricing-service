"""add receivable ledger foundation

Revision ID: f1a2b3c4d5e6
Revises: c2a7f8b4d5e6
Create Date: 2026-03-20 13:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f1a2b3c4d5e6"
down_revision: str | Sequence[str] | None = "c2a7f8b4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "receivable_ledger_event",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("business_key", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("external_document_ref", sa.String(length=64), nullable=False),
        sa.Column("external_document_number", sa.String(length=64), nullable=True),
        sa.Column("external_document_date", sa.DateTime(), nullable=False),
        sa.Column("counterparty_ref", sa.String(length=64), nullable=False),
        sa.Column("counterparty_name", sa.String(length=255), nullable=True),
        sa.Column("manager_ref", sa.String(length=64), nullable=True),
        sa.Column("manager_name", sa.String(length=255), nullable=True),
        sa.Column("store_ref", sa.String(length=64), nullable=True),
        sa.Column("store_name", sa.String(length=255), nullable=True),
        sa.Column("line_no", sa.Integer(), nullable=True),
        sa.Column("amount_delta", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("business_key", name="uq_receivable_ledger_event_business_key"),
    )
    op.create_index(
        "ix_receivable_ledger_event_counterparty_date",
        "receivable_ledger_event",
        ["counterparty_ref", "external_document_date"],
        unique=False,
    )
    op.create_index(
        "ix_receivable_ledger_event_event_type",
        "receivable_ledger_event",
        ["event_type"],
        unique=False,
    )
    op.create_table(
        "counterparty_manager_assignment",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("business_key", sa.String(length=64), nullable=False),
        sa.Column("counterparty_ref", sa.String(length=64), nullable=False),
        sa.Column("counterparty_name", sa.String(length=255), nullable=True),
        sa.Column("manager_ref", sa.String(length=64), nullable=False),
        sa.Column("manager_name", sa.String(length=255), nullable=True),
        sa.Column("effective_from", sa.DateTime(), nullable=False),
        sa.Column("effective_to", sa.DateTime(), nullable=True),
        sa.Column("assignment_reason", sa.String(length=64), nullable=True),
        sa.Column("source_event_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_event_id"],
            ["receivable_ledger_event.id"],
            name="fk_counterparty_manager_assignment_source_event_id",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("business_key", name="uq_counterparty_manager_assignment_business_key"),
    )
    op.create_index(
        "ix_counterparty_manager_assignment_counterparty_from",
        "counterparty_manager_assignment",
        ["counterparty_ref", "effective_from"],
        unique=False,
    )
    op.create_table(
        "receivable_balance_snapshot",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("counterparty_ref", sa.String(length=64), nullable=False),
        sa.Column("counterparty_name", sa.String(length=255), nullable=True),
        sa.Column("current_balance", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("origin_event_id", sa.Integer(), nullable=True),
        sa.Column("origin_document_ref", sa.String(length=64), nullable=True),
        sa.Column("origin_document_number", sa.String(length=64), nullable=True),
        sa.Column("origin_document_date", sa.DateTime(), nullable=True),
        sa.Column("origin_manager_ref", sa.String(length=64), nullable=True),
        sa.Column("origin_manager_name", sa.String(length=255), nullable=True),
        sa.Column("current_manager_ref", sa.String(length=64), nullable=True),
        sa.Column("current_manager_name", sa.String(length=255), nullable=True),
        sa.Column("last_sale_at", sa.DateTime(), nullable=True),
        sa.Column("last_payment_at", sa.DateTime(), nullable=True),
        sa.Column("aged_bucket", sa.String(length=16), nullable=False),
        sa.Column("activity_segment", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["origin_event_id"],
            ["receivable_ledger_event.id"],
            name="fk_receivable_balance_snapshot_origin_event_id",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "snapshot_date",
            "counterparty_ref",
            name="uq_receivable_balance_snapshot_date_counterparty",
        ),
    )
    op.create_index(
        "ix_receivable_balance_snapshot_snapshot_date",
        "receivable_balance_snapshot",
        ["snapshot_date"],
        unique=False,
    )
    op.create_index(
        "ix_receivable_balance_snapshot_activity_segment",
        "receivable_balance_snapshot",
        ["activity_segment"],
        unique=False,
    )
    op.create_index(
        "ix_receivable_balance_snapshot_aged_bucket",
        "receivable_balance_snapshot",
        ["aged_bucket"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_receivable_balance_snapshot_aged_bucket",
        table_name="receivable_balance_snapshot",
    )
    op.drop_index(
        "ix_receivable_balance_snapshot_activity_segment",
        table_name="receivable_balance_snapshot",
    )
    op.drop_index(
        "ix_receivable_balance_snapshot_snapshot_date",
        table_name="receivable_balance_snapshot",
    )
    op.drop_table("receivable_balance_snapshot")
    op.drop_index(
        "ix_counterparty_manager_assignment_counterparty_from",
        table_name="counterparty_manager_assignment",
    )
    op.drop_table("counterparty_manager_assignment")
    op.drop_index(
        "ix_receivable_ledger_event_event_type",
        table_name="receivable_ledger_event",
    )
    op.drop_index(
        "ix_receivable_ledger_event_counterparty_date",
        table_name="receivable_ledger_event",
    )
    op.drop_table("receivable_ledger_event")
