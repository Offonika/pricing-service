"""Add auditable logistics draft cancellation.

Revision ID: a7c9e1f3b5d7
Revises: 9d1f3a5c7e68
Create Date: 2026-08-27
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "a7c9e1f3b5d7"
down_revision = "9d1f3a5c7e68"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("logistics_draft") as batch_op:
        batch_op.add_column(sa.Column("cancelled_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("cancelled_by_user_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("cancel_reason", sa.String(length=1000), nullable=True))
        batch_op.create_foreign_key(
            "fk_logistics_draft_cancelled_by_user_id",
            "logistics_user",
            ["cancelled_by_user_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("logistics_draft") as batch_op:
        batch_op.drop_constraint(
            "fk_logistics_draft_cancelled_by_user_id",
            type_="foreignkey",
        )
        batch_op.drop_column("cancel_reason")
        batch_op.drop_column("cancelled_by_user_id")
        batch_op.drop_column("cancelled_at")
