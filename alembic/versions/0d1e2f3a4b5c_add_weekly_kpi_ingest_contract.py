"""add weekly KPI ingest idempotency contract

Revision ID: 0d1e2f3a4b5c
Revises: 9c0d1e2f3a45
Create Date: 2026-07-12 16:55:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0d1e2f3a4b5c"
down_revision = "9c0d1e2f3a45"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "weekly_kpi_report_snapshot",
        sa.Column("source_content_sha256", sa.String(length=64), nullable=True),
    )
    op.create_table(
        "weekly_kpi_ingest_request",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("result_payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )


def downgrade() -> None:
    op.drop_table("weekly_kpi_ingest_request")
    op.drop_column("weekly_kpi_report_snapshot", "source_content_sha256")
