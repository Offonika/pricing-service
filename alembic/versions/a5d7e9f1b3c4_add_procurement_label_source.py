"""add procurement order label source

Revision ID: a5d7e9f1b3c4
Revises: f5b7c9d1e3a5
Create Date: 2026-08-31 15:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "a5d7e9f1b3c4"
down_revision = "f5b7c9d1e3a5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "procurement_order_formation",
        sa.Column("label_onec_document_number", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "procurement_order_formation",
        sa.Column("label_onec_document_date", sa.Date(), nullable=True),
    )
    op.add_column(
        "procurement_order_formation",
        sa.Column("label_source_linked_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("procurement_order_formation", "label_source_linked_at")
    op.drop_column("procurement_order_formation", "label_onec_document_date")
    op.drop_column("procurement_order_formation", "label_onec_document_number")
