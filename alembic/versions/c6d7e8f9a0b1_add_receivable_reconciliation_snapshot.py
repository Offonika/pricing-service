"""add receivable reconciliation snapshot

Revision ID: c6d7e8f9a0b1
Revises: a1b2c3d4e5f6
Create Date: 2026-03-29 20:45:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c6d7e8f9a0b1"
down_revision: str | Sequence[str] | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "receivable_reconciliation_snapshot",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("counterparty_ref", sa.String(length=64), nullable=False),
        sa.Column("counterparty_name", sa.String(length=255), nullable=True),
        sa.Column("signed_balance", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("absolute_balance", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("current_manager_ref", sa.String(length=64), nullable=True),
        sa.Column("current_manager_name", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "snapshot_date",
            "counterparty_ref",
            name="uq_receivable_reconciliation_snapshot_date_counterparty",
        ),
    )
    op.create_index(
        "ix_receivable_reconciliation_snapshot_snapshot_date",
        "receivable_reconciliation_snapshot",
        ["snapshot_date"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_receivable_reconciliation_snapshot_snapshot_date",
        table_name="receivable_reconciliation_snapshot",
    )
    op.drop_table("receivable_reconciliation_snapshot")
