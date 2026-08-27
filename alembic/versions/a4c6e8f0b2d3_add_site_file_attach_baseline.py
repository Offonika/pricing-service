"""Persist the CRM file baseline for guarded site attachment retries.

Revision ID: a4c6e8f0b2d3
Revises: 9d1f3a5b7c68
Create Date: 2026-08-26
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "a4c6e8f0b2d3"
down_revision = "9d1f3a5b7c68"
branch_labels = None
depends_on = None


def upgrade() -> None:
    baseline_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.add_column(
        "site_service_request_file",
        sa.Column(
            "bitrix_attach_baseline_file_ids",
            baseline_type,
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column(
        "site_service_request_file",
        "bitrix_attach_baseline_file_ids",
    )
