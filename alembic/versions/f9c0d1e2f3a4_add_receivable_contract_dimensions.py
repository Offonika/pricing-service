"""add receivable contract dimensions

Revision ID: f9c0d1e2f3a4
Revises: f8b9c0d1e2f3
Create Date: 2026-03-24 11:20:00.000000
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "f9c0d1e2f3a4"
down_revision = "f8b9c0d1e2f3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "receivable_ledger_event",
        sa.Column("contract_ref", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "receivable_ledger_event",
        sa.Column("contract_name", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "receivable_ledger_event",
        sa.Column("contract_kind_ref", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "receivable_ledger_event",
        sa.Column("contract_kind_name", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "receivable_ledger_event",
        sa.Column(
            "source_layer",
            sa.String(length=32),
            nullable=False,
            server_default="regular_receivables",
        ),
    )


def downgrade() -> None:
    op.drop_column("receivable_ledger_event", "source_layer")
    op.drop_column("receivable_ledger_event", "contract_kind_name")
    op.drop_column("receivable_ledger_event", "contract_kind_ref")
    op.drop_column("receivable_ledger_event", "contract_name")
    op.drop_column("receivable_ledger_event", "contract_ref")
