"""harden site service request delivery state

Revision ID: 5f7a9c1e3b24
Revises: 4e6f80912c45
Create Date: 2026-08-23 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "5f7a9c1e3b24"
down_revision: str | None = "4e6f80912c45"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "site_service_request_case",
        sa.Column("assignment_last_error_code", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "site_service_request_case",
        sa.Column(
            "escalation_timeline_delivered_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "site_service_request_case",
        sa.Column(
            "escalation_notification_delivered_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.execute(
        sa.text(
            "UPDATE site_service_request_case "
            "SET escalation_timeline_delivered_at = escalated_at, "
            "escalation_notification_delivered_at = escalated_at "
            "WHERE escalated_at IS NOT NULL"
        )
    )
    op.execute(
        sa.text(
            "UPDATE site_service_request_command "
            "SET card_action_cleared_at = COALESCE(ack_at, updated_at, created_at) "
            "WHERE card_action_cleared_at IS NULL"
        )
    )


def downgrade() -> None:
    op.drop_column("site_service_request_case", "escalation_notification_delivered_at")
    op.drop_column("site_service_request_case", "escalation_timeline_delivered_at")
    op.drop_column("site_service_request_case", "assignment_last_error_code")
