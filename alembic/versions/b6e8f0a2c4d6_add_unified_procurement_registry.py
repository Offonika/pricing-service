"""add unified procurement order registry facts

Revision ID: b6e8f0a2c4d6
Revises: a5d7e9f1b3c4
Create Date: 2026-09-01 10:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "b6e8f0a2c4d6"
down_revision = "a5d7e9f1b3c4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    table = "procurement_order_formation"
    op.add_column(
        table, sa.Column("lifecycle_status", sa.String(32), nullable=False, server_default="draft")
    )
    op.add_column(
        table, sa.Column("origin", sa.String(32), nullable=False, server_default="generated")
    )
    op.add_column(table, sa.Column("onec_posted", sa.Boolean(), nullable=True))
    op.add_column(table, sa.Column("onec_marked", sa.Boolean(), nullable=True))
    op.add_column(table, sa.Column("supplier_dispatch_date", sa.Date(), nullable=True))
    op.add_column(table, sa.Column("cargo_dropoff_date", sa.Date(), nullable=True))
    op.add_column(table, sa.Column("expected_receipt_date", sa.Date(), nullable=True))
    op.add_column(table, sa.Column("onec_ordered_quantity", sa.Numeric(18, 3), nullable=True))
    op.add_column(table, sa.Column("onec_open_quantity", sa.Numeric(18, 3), nullable=True))
    op.add_column(table, sa.Column("onec_received_quantity", sa.Numeric(18, 3), nullable=True))
    op.add_column(table, sa.Column("onec_snapshot_hash", sa.String(64), nullable=True))
    op.add_column(table, sa.Column("last_onec_sync_at", sa.DateTime(), nullable=True))
    op.add_column(table, sa.Column("last_onec_seen_at", sa.DateTime(), nullable=True))
    op.add_column(table, sa.Column("sync_conflict", sa.Text(), nullable=True))
    op.execute("""
        UPDATE procurement_order_formation
        SET lifecycle_status = CASE
          WHEN status = 'draft' THEN 'draft'
          WHEN status IN ('review', 'approved', 'error') THEN 'review'
          WHEN status = 'transmitting' THEN 'transmitting'
          WHEN status = 'transmitted' AND onec_document_number IS NOT NULL THEN 'active'
          WHEN status = 'superseded' THEN 'cancelled'
          ELSE 'review'
        END
        """)
    op.create_index("ix_proc_order_formation_lifecycle", table, ["lifecycle_status", "order_date"])
    op.create_index("ix_proc_order_formation_origin", table, ["origin", "order_date"])
    op.create_index("ix_proc_order_formation_onec_ref", table, ["onec_document_ref"])

    line_table = "procurement_order_formation_line"
    op.add_column(line_table, sa.Column("onec_open_quantity", sa.Numeric(18, 3), nullable=True))
    op.add_column(line_table, sa.Column("onec_received_quantity", sa.Numeric(18, 3), nullable=True))


def downgrade() -> None:
    line_table = "procurement_order_formation_line"
    op.drop_column(line_table, "onec_received_quantity")
    op.drop_column(line_table, "onec_open_quantity")

    table = "procurement_order_formation"
    op.drop_index("ix_proc_order_formation_onec_ref", table_name=table)
    op.drop_index("ix_proc_order_formation_origin", table_name=table)
    op.drop_index("ix_proc_order_formation_lifecycle", table_name=table)
    for column in (
        "sync_conflict",
        "last_onec_seen_at",
        "last_onec_sync_at",
        "onec_snapshot_hash",
        "onec_received_quantity",
        "onec_open_quantity",
        "onec_ordered_quantity",
        "expected_receipt_date",
        "cargo_dropoff_date",
        "supplier_dispatch_date",
        "onec_marked",
        "onec_posted",
        "origin",
        "lifecycle_status",
    ):
        op.drop_column(table, column)
