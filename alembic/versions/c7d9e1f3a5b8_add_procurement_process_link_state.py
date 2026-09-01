"""add procurement smart-process link state

Revision ID: c7d9e1f3a5b8
Revises: b6e8f0a2c4d6
Create Date: 2026-09-01 23:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "c7d9e1f3a5b8"
down_revision = "b6e8f0a2c4d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    table = "procurement_order_formation"
    op.add_column(table, sa.Column("bitrix_stage_name", sa.String(length=255), nullable=True))
    op.add_column(table, sa.Column("bitrix_link_checked_at", sa.DateTime(), nullable=True))
    op.add_column(table, sa.Column("bitrix_link_error", sa.Text(), nullable=True))


def downgrade() -> None:
    table = "procurement_order_formation"
    op.drop_column(table, "bitrix_link_error")
    op.drop_column(table, "bitrix_link_checked_at")
    op.drop_column(table, "bitrix_stage_name")
