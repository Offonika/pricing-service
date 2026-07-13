"""verify service accrual contract balance scope

Revision ID: cd34ef56ab78
Revises: bc23de45fa67
Create Date: 2026-07-12 22:05:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "cd34ef56ab78"
down_revision = "bc23de45fa67"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "executive_service_accrual_rule",
        sa.Column(
            "balance_scope_verified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.alter_column(
        "executive_service_accrual_rule",
        "balance_scope_verified",
        server_default=None,
    )


def downgrade() -> None:
    op.drop_column("executive_service_accrual_rule", "balance_scope_verified")
