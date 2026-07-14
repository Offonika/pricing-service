"""add counterparty folder snapshot

Revision ID: 3a4b5c6d7e8f
Revises: 2f7a8c9d0e1f
Create Date: 2026-06-13 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3a4b5c6d7e8f"
down_revision: str | Sequence[str] | None = "2f7a8c9d0e1f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "counterparty_folder_snapshot",
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("counterparty_ref", sa.String(length=64), nullable=False),
        sa.Column("counterparty_name", sa.String(length=255), nullable=True),
        sa.Column("current_folder_ref", sa.String(length=64), nullable=True),
        sa.Column("current_folder_name", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "snapshot_date",
            "counterparty_ref",
            name="uq_counterparty_folder_snapshot_date_ref",
        ),
    )
    op.create_index(
        "ix_counterparty_folder_snapshot_date",
        "counterparty_folder_snapshot",
        ["snapshot_date"],
        unique=False,
    )
    op.create_index(
        "ix_counterparty_folder_snapshot_counterparty_ref",
        "counterparty_folder_snapshot",
        ["counterparty_ref"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_counterparty_folder_snapshot_counterparty_ref",
        table_name="counterparty_folder_snapshot",
    )
    op.drop_index(
        "ix_counterparty_folder_snapshot_date",
        table_name="counterparty_folder_snapshot",
    )
    op.drop_table("counterparty_folder_snapshot")
