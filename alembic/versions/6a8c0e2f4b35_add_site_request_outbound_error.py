"""add site request outbound error checkpoint

Revision ID: 6a8c0e2f4b35
Revises: 5f7a9c1e3b24
Create Date: 2026-08-23 18:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "6a8c0e2f4b35"
down_revision: str | None = "5f7a9c1e3b24"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "site_service_request_case",
        sa.Column("outbound_last_error_code", sa.String(length=128), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("site_service_request_case", "outbound_last_error_code")
