"""add receivable credit terms

Revision ID: f8b9c0d1e2f3
Revises: f4d5e6f7a8b9
Create Date: 2026-03-20 22:40:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f8b9c0d1e2f3"
down_revision: str | Sequence[str] | None = "f4d5e6f7a8b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "receivable_ledger_event",
        sa.Column("planned_payment_date", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "receivable_ledger_event",
        sa.Column("credit_depth_days", sa.Integer(), nullable=True),
    )
    op.add_column(
        "receivable_ledger_event",
        sa.Column("shipment_ban", sa.Boolean(), nullable=True),
    )

    op.add_column(
        "receivable_balance_snapshot",
        sa.Column("planned_payment_date", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "receivable_balance_snapshot",
        sa.Column("credit_depth_days", sa.Integer(), nullable=True),
    )
    op.add_column(
        "receivable_balance_snapshot",
        sa.Column("shipment_ban", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "receivable_balance_snapshot",
        sa.Column("payment_term_source", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "receivable_balance_snapshot", sa.Column("due_date", sa.DateTime(), nullable=True)
    )
    op.add_column(
        "receivable_balance_snapshot",
        sa.Column("overdue_days", sa.Integer(), nullable=True),
    )
    op.add_column(
        "receivable_balance_snapshot",
        sa.Column(
            "is_overdue",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )

    op.add_column(
        "receivable_case", sa.Column("planned_payment_date", sa.DateTime(), nullable=True)
    )
    op.add_column("receivable_case", sa.Column("credit_depth_days", sa.Integer(), nullable=True))
    op.add_column("receivable_case", sa.Column("shipment_ban", sa.Boolean(), nullable=True))
    op.add_column(
        "receivable_case",
        sa.Column("payment_term_source", sa.String(length=32), nullable=True),
    )
    op.add_column("receivable_case", sa.Column("due_date", sa.DateTime(), nullable=True))
    op.add_column("receivable_case", sa.Column("overdue_days", sa.Integer(), nullable=True))
    op.add_column(
        "receivable_case",
        sa.Column(
            "is_overdue",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("receivable_case", "is_overdue")
    op.drop_column("receivable_case", "overdue_days")
    op.drop_column("receivable_case", "due_date")
    op.drop_column("receivable_case", "payment_term_source")
    op.drop_column("receivable_case", "shipment_ban")
    op.drop_column("receivable_case", "credit_depth_days")
    op.drop_column("receivable_case", "planned_payment_date")

    op.drop_column("receivable_balance_snapshot", "is_overdue")
    op.drop_column("receivable_balance_snapshot", "overdue_days")
    op.drop_column("receivable_balance_snapshot", "due_date")
    op.drop_column("receivable_balance_snapshot", "payment_term_source")
    op.drop_column("receivable_balance_snapshot", "shipment_ban")
    op.drop_column("receivable_balance_snapshot", "credit_depth_days")
    op.drop_column("receivable_balance_snapshot", "planned_payment_date")

    op.drop_column("receivable_ledger_event", "shipment_ban")
    op.drop_column("receivable_ledger_event", "credit_depth_days")
    op.drop_column("receivable_ledger_event", "planned_payment_date")
