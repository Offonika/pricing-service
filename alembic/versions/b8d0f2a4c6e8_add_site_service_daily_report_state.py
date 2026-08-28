"""add site service request daily report state

Revision ID: b8d0f2a4c6e8
Revises: a7c9e1f3b5d7
Create Date: 2026-08-28
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "b8d0f2a4c6e8"
down_revision = "a7c9e1f3b5d7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "site_service_request_worker_state",
        sa.Column("last_daily_report_date", sa.Date(), nullable=True),
    )
    op.add_column(
        "site_service_request_worker_state",
        sa.Column("last_daily_report_message_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "site_service_request_worker_state",
        sa.Column(
            "last_daily_report_delivered_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column(
        "site_service_request_worker_state",
        "last_daily_report_delivered_at",
    )
    op.drop_column(
        "site_service_request_worker_state",
        "last_daily_report_message_id",
    )
    op.drop_column(
        "site_service_request_worker_state",
        "last_daily_report_date",
    )
