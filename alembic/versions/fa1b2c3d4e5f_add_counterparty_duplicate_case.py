"""add counterparty duplicate case outbox"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "fa1b2c3d4e5f"
down_revision: str | None = "f9c0d1e2f3a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "counterparty_duplicate_case",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("dedupe_key", sa.String(length=64), nullable=False),
        sa.Column("detected_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("risk_level", sa.String(length=8), nullable=False),
        sa.Column("reason_codes", sa.JSON(), nullable=False),
        sa.Column("candidate_records", sa.JSON(), nullable=False),
        sa.Column("responsible_code", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="new"),
        sa.Column("sla_deadline_at", sa.DateTime(), nullable=False),
        sa.Column("summary_text", sa.Text(), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("delivery_state", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("delivered_at", sa.DateTime(), nullable=True),
        sa.Column("last_notified_at", sa.DateTime(), nullable=True),
        sa.Column("external_case_id", sa.String(length=128), nullable=True),
        sa.Column("external_status", sa.String(length=64), nullable=True),
        sa.Column("external_url", sa.String(length=1024), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_counterparty_duplicate_case_dedupe_key",
        "counterparty_duplicate_case",
        ["dedupe_key"],
        unique=True,
    )
    op.create_index(
        "ix_counterparty_duplicate_case_delivery_state_detected_at",
        "counterparty_duplicate_case",
        ["delivery_state", "detected_at"],
        unique=False,
    )
    op.create_index(
        "ix_counterparty_duplicate_case_last_seen_at",
        "counterparty_duplicate_case",
        ["last_seen_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_counterparty_duplicate_case_last_seen_at",
        table_name="counterparty_duplicate_case",
    )
    op.drop_index(
        "ix_counterparty_duplicate_case_delivery_state_detected_at",
        table_name="counterparty_duplicate_case",
    )
    op.drop_index(
        "ix_counterparty_duplicate_case_dedupe_key",
        table_name="counterparty_duplicate_case",
    )
    op.drop_table("counterparty_duplicate_case")
