"""finalize site request concurrency and delivery hardening

Revision ID: 7b9d1f3a5c46
Revises: 6a8c0e2f4b35
Create Date: 2026-08-23 21:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "7b9d1f3a5c46"
down_revision: str | None = "6a8c0e2f4b35"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "site_service_request_event",
        sa.Column(
            "consecutive_permanent_failures",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    if op.get_bind().dialect.name != "sqlite":
        op.create_check_constraint(
            "ck_site_service_request_event_permanent_failures",
            "site_service_request_event",
            "consecutive_permanent_failures >= 0",
        )
    op.add_column(
        "site_service_request_event",
        sa.Column("source_message_sha256", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "site_service_request_command",
        sa.Column("lease_token", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "site_service_request_file",
        sa.Column("bitrix_error_reported_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_site_service_request_command_case",
        "site_service_request_command",
        ["case_id"],
        unique=False,
    )

    # The previous application version delivered only the escalation timeline,
    # not the personal notification. Let the hardened worker deliver the missing
    # notification instead of treating it as already confirmed.
    op.execute(
        sa.text(
            "UPDATE site_service_request_case "
            "SET escalation_notification_delivered_at = NULL "
            "WHERE escalated_at IS NOT NULL"
        )
    )
    # The previous backfill marked every pre-existing command as already cleared,
    # although the old worker did not clear SEND at all. Only the newest command
    # may still own the current card action; older commands must remain cleared so
    # later ticks cannot regress the card to an obsolete terminal status.
    op.execute(
        sa.text(
            "UPDATE site_service_request_command "
            "SET card_action_cleared_at = NULL "
            "WHERE id IN ("
            "  SELECT id FROM ("
            "    SELECT id, ROW_NUMBER() OVER ("
            "      PARTITION BY case_id ORDER BY created_at DESC, id DESC"
            "    ) AS row_number "
            "    FROM site_service_request_command"
            "  ) AS ranked_commands "
            "  WHERE row_number = 1"
            ")"
        )
    )
    # Rebuild health from the most recent terminal delivery: preserve a failure
    # even when a newer replacement is pending/leased, and clear a stale failure
    # when the most recent terminal command was applied.
    op.execute(
        sa.text(
            "WITH latest_terminal_command AS ("
            "  SELECT case_id, status, last_error_code, updated_at, "
            "         ROW_NUMBER() OVER ("
            "           PARTITION BY case_id ORDER BY created_at DESC, id DESC"
            "         ) AS row_number "
            "  FROM site_service_request_command "
            "  WHERE status IN ('applied', 'failed')"
            ") "
            "UPDATE site_service_request_case AS request_case "
            "SET outbound_last_error_code = CASE "
            "      WHEN latest_terminal_command.status = 'failed' "
            "      THEN COALESCE("
            "        latest_terminal_command.last_error_code, "
            "        'outbound_delivery_failed'"
            "      ) "
            "      ELSE NULL "
            "    END, "
            "    outbound_checked_at = CASE "
            "      WHEN request_case.outbound_checked_at IS NULL "
            "        OR request_case.outbound_checked_at "
            "           < latest_terminal_command.updated_at "
            "      THEN latest_terminal_command.updated_at "
            "      ELSE request_case.outbound_checked_at "
            "    END "
            "FROM latest_terminal_command "
            "WHERE latest_terminal_command.case_id = request_case.id "
            "  AND latest_terminal_command.row_number = 1"
        )
    )


def downgrade() -> None:
    op.drop_index(
        "ix_site_service_request_command_case",
        table_name="site_service_request_command",
    )
    op.drop_column("site_service_request_command", "lease_token")
    op.drop_column("site_service_request_file", "bitrix_error_reported_at")
    if op.get_bind().dialect.name != "sqlite":
        op.drop_constraint(
            "ck_site_service_request_event_permanent_failures",
            "site_service_request_event",
            type_="check",
        )
    op.drop_column("site_service_request_event", "source_message_sha256")
    op.drop_column("site_service_request_event", "consecutive_permanent_failures")
