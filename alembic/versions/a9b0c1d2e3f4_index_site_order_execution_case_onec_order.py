"""Index the 1C order identifier used by the KMP4 state projection.

Revision ID: a9b0c1d2e3f4
Revises: e8f9012345a6
Create Date: 2026-09-04
"""

from __future__ import annotations

from alembic import op

revision = "a9b0c1d2e3f4"
down_revision = "e8f9012345a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_site_order_execution_case_onec_order",
        "site_order_execution_case",
        ["onec_order_external_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_site_order_execution_case_onec_order",
        table_name="site_order_execution_case",
    )
