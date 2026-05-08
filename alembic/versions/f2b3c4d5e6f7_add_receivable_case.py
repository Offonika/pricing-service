"""add receivable case

Revision ID: f2b3c4d5e6f7
Revises: f1a2b3c4d5e6
Create Date: 2026-03-20 14:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2b3c4d5e6f7"
down_revision: str | Sequence[str] | None = "f1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "receivable_case",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("segment", sa.String(length=32), nullable=False),
        sa.Column("owner_type", sa.String(length=32), nullable=False),
        sa.Column("recommendation", sa.String(length=255), nullable=False),
        sa.Column("counterparty_ref", sa.String(length=64), nullable=False),
        sa.Column("counterparty_name", sa.String(length=255), nullable=True),
        sa.Column("current_balance", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("aged_bucket", sa.String(length=16), nullable=False),
        sa.Column("activity_segment", sa.String(length=16), nullable=False),
        sa.Column("origin_document_ref", sa.String(length=64), nullable=True),
        sa.Column("origin_document_number", sa.String(length=64), nullable=True),
        sa.Column("origin_document_date", sa.DateTime(), nullable=True),
        sa.Column("origin_manager_ref", sa.String(length=64), nullable=True),
        sa.Column("origin_manager_name", sa.String(length=255), nullable=True),
        sa.Column("current_manager_ref", sa.String(length=64), nullable=True),
        sa.Column("current_manager_name", sa.String(length=255), nullable=True),
        sa.Column("chain_documents", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "snapshot_date",
            "segment",
            "counterparty_ref",
            name="uq_receivable_case_date_segment_counterparty",
        ),
    )
    op.create_index(
        "ix_receivable_case_snapshot_date",
        "receivable_case",
        ["snapshot_date"],
        unique=False,
    )
    op.create_index("ix_receivable_case_segment", "receivable_case", ["segment"], unique=False)
    op.create_index(
        "ix_receivable_case_owner_type",
        "receivable_case",
        ["owner_type"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_receivable_case_owner_type", table_name="receivable_case")
    op.drop_index("ix_receivable_case_segment", table_name="receivable_case")
    op.drop_index("ix_receivable_case_snapshot_date", table_name="receivable_case")
    op.drop_table("receivable_case")
