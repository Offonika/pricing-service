"""harden site service request worker state

Revision ID: 4e6f80912c45
Revises: 3d5e7f901b34
Create Date: 2026-08-22 18:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "4e6f80912c45"
down_revision: str | None = "3d5e7f901b34"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "site_service_request_case",
        sa.Column(
            "base_sync_status",
            sa.String(length=32),
            nullable=False,
            server_default="pending",
        ),
    )
    op.add_column(
        "site_service_request_case",
        sa.Column("base_error_code", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "site_service_request_case",
        sa.Column("assignment_checked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "site_service_request_case",
        sa.Column("outbound_checked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE site_service_request_case "
            "SET base_sync_status = sync_status, base_error_code = last_error_code"
        )
    )
    op.create_index(
        "ix_site_service_request_case_assignment_checked",
        "site_service_request_case",
        ["assignment_checked_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_site_service_request_case_outbound_checked",
        "site_service_request_case",
        ["outbound_checked_at", "id"],
        unique=False,
    )
    op.add_column(
        "site_service_request_command",
        sa.Column("card_action_cleared_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("site_service_request_command", "card_action_cleared_at")
    op.drop_index(
        "ix_site_service_request_case_outbound_checked",
        table_name="site_service_request_case",
    )
    op.drop_index(
        "ix_site_service_request_case_assignment_checked",
        table_name="site_service_request_case",
    )
    op.drop_column("site_service_request_case", "outbound_checked_at")
    op.drop_column("site_service_request_case", "assignment_checked_at")
    op.drop_column("site_service_request_case", "base_error_code")
    op.drop_column("site_service_request_case", "base_sync_status")
