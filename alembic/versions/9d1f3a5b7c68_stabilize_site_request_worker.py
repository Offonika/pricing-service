"""Stabilize the site service request worker.

Revision ID: 9d1f3a5b7c68
Revises: 8c0e2a4b6d57
Create Date: 2026-08-26
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "9d1f3a5b7c68"
down_revision = "8c0e2a4b6d57"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "site_service_request_file",
        sa.Column(
            "bitrix_attach_attempted_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_table(
        "site_service_request_worker_state",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("last_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=128), nullable=True),
        sa.Column(
            "consecutive_failures",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "id = 1",
            name="ck_site_service_request_worker_state_singleton",
        ),
        sa.CheckConstraint(
            "consecutive_failures >= 0",
            name="ck_site_service_request_worker_state_failures",
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("site_service_request_worker_state")
    op.drop_column("site_service_request_file", "bitrix_attach_attempted_at")
