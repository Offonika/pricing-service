"""add receivable bitrix contour links

Revision ID: f4a5b6c7d8e9
Revises: e2b3c4d5e6f8
Create Date: 2026-08-01 20:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f4a5b6c7d8e9"
down_revision: str | Sequence[str] | None = "e2b3c4d5e6f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "receivable_bitrix_link",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("work_item_id", sa.Integer(), nullable=False),
        sa.Column("contour_code", sa.String(length=64), nullable=False),
        sa.Column("entity_type_id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.String(length=64), nullable=False),
        sa.Column("detail_url", sa.String(length=512), nullable=True),
        sa.Column("stage_id", sa.String(length=128), nullable=True),
        sa.Column("last_sync_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["work_item_id"], ["receivable_work_item.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "work_item_id", "contour_code", name="uq_receivable_bitrix_link_work_contour"
        ),
        sa.UniqueConstraint(
            "contour_code",
            "entity_type_id",
            "item_id",
            name="uq_receivable_bitrix_link_contour_item",
        ),
    )
    op.create_index(
        "ix_receivable_bitrix_link_work_item_id", "receivable_bitrix_link", ["work_item_id"]
    )
    op.create_index(
        "ix_receivable_bitrix_link_contour_code", "receivable_bitrix_link", ["contour_code"]
    )
    op.create_table(
        "receivable_folder_change_operation",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("signal_key", sa.String(length=64), nullable=False),
        sa.Column("counterparty_ref", sa.String(length=64), nullable=False),
        sa.Column("counterparty_code", sa.String(length=32), nullable=True),
        sa.Column("counterparty_name", sa.String(length=255), nullable=True),
        sa.Column("active_counterparty_key", sa.String(length=64), nullable=True),
        sa.Column("expected_old_folder_ref", sa.String(length=64), nullable=False),
        sa.Column("expected_old_folder_name", sa.String(length=255), nullable=True),
        sa.Column("proposed_new_folder_ref", sa.String(length=64), nullable=False),
        sa.Column("proposed_new_folder_name", sa.String(length=255), nullable=True),
        sa.Column("signal_snapshot", sa.JSON(), nullable=False),
        sa.Column("data_version", sa.String(length=96), nullable=False),
        sa.Column("decision_hash", sa.String(length=64), nullable=True),
        sa.Column("approved_by_bitrix_user_id", sa.String(length=32), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("dry_run_message_id", sa.String(length=120), nullable=True),
        sa.Column("apply_message_id", sa.String(length=120), nullable=True),
        sa.Column("readback_folder_ref", sa.String(length=64), nullable=True),
        sa.Column("readback_folder_name", sa.String(length=255), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "state IN ('draft','dry_run_sent','dry_run_ok','apply_sent','applied',"
            "'failed','needs_review')",
            name="ck_receivable_folder_change_state",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "active_counterparty_key",
            name="uq_receivable_folder_change_active_counterparty",
        ),
    )
    op.create_index(
        "ix_receivable_folder_change_state_updated",
        "receivable_folder_change_operation",
        ["state", "updated_at"],
    )
    op.create_index(
        "ix_receivable_folder_change_signal_key",
        "receivable_folder_change_operation",
        ["signal_key"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_receivable_folder_change_signal_key",
        table_name="receivable_folder_change_operation",
    )
    op.drop_index(
        "ix_receivable_folder_change_state_updated",
        table_name="receivable_folder_change_operation",
    )
    op.drop_table("receivable_folder_change_operation")
    op.drop_index("ix_receivable_bitrix_link_contour_code", table_name="receivable_bitrix_link")
    op.drop_index("ix_receivable_bitrix_link_work_item_id", table_name="receivable_bitrix_link")
    op.drop_table("receivable_bitrix_link")
