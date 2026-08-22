"""add receivable PKO shadow results

Revision ID: c4e6f8a0b2d4
Revises: b2d4f6a8c0e1
Create Date: 2026-08-10 15:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c4e6f8a0b2d4"
down_revision: str | Sequence[str] | None = "b2d4f6a8c0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "receivable_pko_shadow_result",
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("algorithm_version", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("counterparty_ref", sa.String(length=64), nullable=False),
        sa.Column("counterparty_code", sa.String(length=32), nullable=True),
        sa.Column("counterparty_name", sa.String(length=255), nullable=True),
        sa.Column("department_ref", sa.String(length=64), nullable=True),
        sa.Column("department_name", sa.String(length=255), nullable=True),
        sa.Column("current_balance", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("base_payment_ref", sa.String(length=64), nullable=True),
        sa.Column("base_payment_number", sa.String(length=64), nullable=True),
        sa.Column("base_payment_date", sa.DateTime(), nullable=True),
        sa.Column("base_balance_after", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("current_origin_document_ref", sa.String(length=64), nullable=True),
        sa.Column("current_origin_document_number", sa.String(length=64), nullable=True),
        sa.Column("current_origin_document_date", sa.DateTime(), nullable=True),
        sa.Column("candidate_origin_document_ref", sa.String(length=64), nullable=True),
        sa.Column("candidate_origin_document_number", sa.String(length=64), nullable=True),
        sa.Column("candidate_origin_document_date", sa.DateTime(), nullable=True),
        sa.Column("candidate_responsible_ref", sa.String(length=64), nullable=True),
        sa.Column("candidate_responsible_name", sa.String(length=255), nullable=True),
        sa.Column(
            "candidate_origin_open_amount",
            sa.Numeric(precision=18, scale=2),
            nullable=True,
        ),
        sa.Column("selected_open_amount", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("delta", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("current_documents", sa.JSON(), nullable=False),
        sa.Column("candidate_documents", sa.JSON(), nullable=False),
        sa.Column("diagnostics", sa.JSON(), nullable=False),
        sa.Column(
            "computed_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "snapshot_date",
            "algorithm_version",
            "counterparty_ref",
            name="uq_receivable_pko_shadow_date_version_counterparty",
        ),
    )
    op.create_index(
        "ix_receivable_pko_shadow_date_version",
        "receivable_pko_shadow_result",
        ["snapshot_date", "algorithm_version"],
        unique=False,
    )
    op.create_index(
        "ix_receivable_pko_shadow_run_id",
        "receivable_pko_shadow_result",
        ["run_id"],
        unique=False,
    )
    op.create_index(
        "ix_receivable_pko_shadow_status",
        "receivable_pko_shadow_result",
        ["status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_receivable_pko_shadow_status", table_name="receivable_pko_shadow_result")
    op.drop_index("ix_receivable_pko_shadow_run_id", table_name="receivable_pko_shadow_result")
    op.drop_index(
        "ix_receivable_pko_shadow_date_version",
        table_name="receivable_pko_shadow_result",
    )
    op.drop_table("receivable_pko_shadow_result")
