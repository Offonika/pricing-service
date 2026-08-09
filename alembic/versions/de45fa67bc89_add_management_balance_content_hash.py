"""add idempotency hash to management balance snapshots

Revision ID: de45fa67bc89
Revises: cd34ef56ab78
Create Date: 2026-07-13 15:20:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "de45fa67bc89"
down_revision = "cd34ef56ab78"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "executive_management_balance_snapshot",
        sa.Column("content_sha256", sa.String(length=64), nullable=True),
    )
    op.create_unique_constraint(
        "uq_executive_management_balance_date_view_content",
        "executive_management_balance_snapshot",
        ["balance_date", "view_mode", "content_sha256"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_executive_management_balance_date_view_content",
        "executive_management_balance_snapshot",
        type_="unique",
    )
    op.drop_column("executive_management_balance_snapshot", "content_sha256")
