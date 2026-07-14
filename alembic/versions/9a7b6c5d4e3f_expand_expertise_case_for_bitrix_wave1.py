"""expand expertise case for bitrix wave1

Revision ID: 9a7b6c5d4e3f
Revises: 8d41b2e6c7f1
Create Date: 2026-04-11 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9a7b6c5d4e3f"
down_revision: str | Sequence[str] | None = "8d41b2e6c7f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "expertise_case",
        sa.Column("organization_ref", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "expertise_case",
        sa.Column("contract_ref", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "expertise_case",
        sa.Column("linked_sale_ref", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "expertise_case",
        sa.Column("decision_label", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "expertise_case",
        sa.Column("bitrix_disk_folder_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "expertise_case",
        sa.Column("bitrix_disk_folder_url", sa.String(length=1000), nullable=True),
    )
    op.add_column(
        "expertise_case",
        sa.Column("bitrix_notify_task_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "expertise_case",
        sa.Column("bitrix_last_sync_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "expertise_case",
        sa.Column("bitrix_last_error", sa.String(length=1000), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("expertise_case", "bitrix_last_error")
    op.drop_column("expertise_case", "bitrix_last_sync_at")
    op.drop_column("expertise_case", "bitrix_notify_task_id")
    op.drop_column("expertise_case", "bitrix_disk_folder_url")
    op.drop_column("expertise_case", "bitrix_disk_folder_id")
    op.drop_column("expertise_case", "decision_label")
    op.drop_column("expertise_case", "linked_sale_ref")
    op.drop_column("expertise_case", "contract_ref")
    op.drop_column("expertise_case", "organization_ref")
