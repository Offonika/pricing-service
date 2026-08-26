"""Add source identities for service email intake.

Revision ID: 8c0e2a4b6d57
Revises: 3e7a9c1d5f24
Create Date: 2026-08-25
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "8c0e2a4b6d57"
down_revision = "3e7a9c1d5f24"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "site_service_request_case",
        sa.Column(
            "source_kind",
            sa.String(length=32),
            nullable=False,
            server_default="site_ticket",
        ),
    )
    op.add_column(
        "site_service_request_case",
        sa.Column("source_key", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "site_service_request_case",
        sa.Column("source_mailbox", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "site_service_request_case",
        sa.Column("source_thread_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "site_service_request_case",
        sa.Column("primary_activity_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "site_service_request_case",
        sa.Column("deal_manager_user_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "site_service_request_case",
        sa.Column("deal_manager_notified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_site_service_request_case_source",
        "site_service_request_case",
        ["source_kind", "source_key"],
        unique=False,
    )
    op.add_column(
        "site_service_request_event",
        sa.Column("source_activity_id", sa.BigInteger(), nullable=True),
    )

    op.create_table(
        "site_service_request_source",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("case_id", sa.Integer(), nullable=False),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("source_key", sa.String(length=255), nullable=False),
        sa.Column("source_mailbox", sa.String(length=32), nullable=True),
        sa.Column("source_thread_id", sa.BigInteger(), nullable=True),
        sa.Column("primary_activity_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["case_id"],
            ["site_service_request_case.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_kind",
            "source_key",
            name="uq_site_service_request_source_identity",
        ),
    )
    op.create_index(
        "ix_site_service_request_source_case",
        "site_service_request_source",
        ["case_id", "source_kind"],
        unique=False,
    )

    bind = op.get_bind()
    if bind.dialect.name in {"mysql", "mariadb"}:
        key_expression = "CONCAT('site-support-ticket:', source_ticket_id)"
    else:
        key_expression = "'site-support-ticket:' || CAST(source_ticket_id AS VARCHAR)"
    op.execute(
        sa.text(
            "UPDATE site_service_request_case "
            f"SET source_key = {key_expression} "
            "WHERE source_key IS NULL"
        )
    )
    op.execute(
        sa.text(
            "INSERT INTO site_service_request_source "
            "(case_id, source_kind, source_key, created_at, updated_at) "
            "SELECT id, source_kind, source_key, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP "
            "FROM site_service_request_case WHERE source_key IS NOT NULL"
        )
    )


def downgrade() -> None:
    op.drop_index(
        "ix_site_service_request_source_case",
        table_name="site_service_request_source",
    )
    op.drop_table("site_service_request_source")
    op.drop_column("site_service_request_event", "source_activity_id")
    op.drop_index(
        "ix_site_service_request_case_source",
        table_name="site_service_request_case",
    )
    op.drop_column("site_service_request_case", "deal_manager_notified_at")
    op.drop_column("site_service_request_case", "deal_manager_user_id")
    op.drop_column("site_service_request_case", "primary_activity_id")
    op.drop_column("site_service_request_case", "source_thread_id")
    op.drop_column("site_service_request_case", "source_mailbox")
    op.drop_column("site_service_request_case", "source_key")
    op.drop_column("site_service_request_case", "source_kind")
