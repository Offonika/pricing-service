"""bind customer settlement reconciliation to its validated context

Revision ID: 6e8f0a2b4c6d
Revises: 4c6e8a0b2d3f
Create Date: 2026-08-23 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "6e8f0a2b4c6d"
down_revision = "4c6e8a0b2d3f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("customer_settlement_reconciliation_run") as batch:
        batch.add_column(sa.Column("context_hash", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("source_hash", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("input_hash", sa.String(length=64), nullable=True))
        batch.drop_constraint("uq_customer_settlement_report_run", type_="unique")
        batch.create_check_constraint(
            "ck_customer_settlement_reconciliation_hashes",
            "(context_hash IS NULL OR length(context_hash) = 64) AND "
            "(source_hash IS NULL OR length(source_hash) = 64) AND "
            "(input_hash IS NULL OR length(input_hash) = 64)",
        )
        batch.create_check_constraint(
            "ck_customer_settlement_reconciliation_totals",
            "expected_count = matched_count + mismatch_count " "AND max_abs_difference >= 0",
        )
        batch.create_check_constraint(
            "ck_customer_settlement_reconciliation_status_counts",
            "(status = 'matched' AND mismatch_count = 0) OR "
            "(status = 'mismatched' AND mismatch_count > 0) OR status = 'blocked'",
        )
        batch.create_unique_constraint(
            "uq_customer_settlement_reconciliation_input",
            ["input_hash"],
        )


def downgrade() -> None:
    op.execute(sa.text("""
            DELETE FROM customer_settlement_reconciliation_run
            WHERE id IN (
                SELECT id
                FROM (
                    SELECT
                        id,
                        ROW_NUMBER() OVER (
                            PARTITION BY report_date, report_hash
                            ORDER BY id DESC
                        ) AS duplicate_rank
                    FROM customer_settlement_reconciliation_run
                ) AS ranked
                WHERE duplicate_rank > 1
            )
            """))
    with op.batch_alter_table("customer_settlement_reconciliation_run") as batch:
        batch.drop_constraint("uq_customer_settlement_reconciliation_input", type_="unique")
        batch.drop_constraint(
            "ck_customer_settlement_reconciliation_status_counts",
            type_="check",
        )
        batch.drop_constraint("ck_customer_settlement_reconciliation_totals", type_="check")
        batch.drop_constraint("ck_customer_settlement_reconciliation_hashes", type_="check")
        batch.create_unique_constraint(
            "uq_customer_settlement_report_run",
            ["report_date", "report_hash"],
        )
        batch.drop_column("input_hash")
        batch.drop_column("source_hash")
        batch.drop_column("context_hash")
