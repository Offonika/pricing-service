"""remember the last non-terminal site service request stage

Revision ID: 3d5e7f901b34
Revises: 2c4d6e8f0a12
Create Date: 2026-08-22 16:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "3d5e7f901b34"
down_revision: str | None = "2c4d6e8f0a12"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "site_service_request_case",
        sa.Column("last_open_stage_id", sa.String(length=128), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("site_service_request_case", "last_open_stage_id")
