"""add receivable supervisor notes

Revision ID: a0b1c2d3e4f6
Revises: f9a0b1c2d3e4
Create Date: 2026-08-05 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a0b1c2d3e4f6"
down_revision: str | Sequence[str] | None = "f9a0b1c2d3e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "receivable_supervisor_note",
        sa.Column("work_item_id", sa.Integer(), nullable=False),
        sa.Column("author_bitrix_user_id", sa.String(length=64), nullable=False),
        sa.Column("author_name", sa.String(length=255), nullable=False),
        sa.Column("visibility", sa.String(length=16), nullable=False),
        sa.Column("comment", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.CheckConstraint(
            "visibility IN ('personal', 'shared')",
            name="ck_receivable_supervisor_note_visibility",
        ),
        sa.ForeignKeyConstraint(
            ["work_item_id"],
            ["receivable_work_item.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "work_item_id",
            "author_bitrix_user_id",
            "visibility",
            name="uq_receivable_supervisor_note_author_visibility",
        ),
    )
    op.create_index(
        "ix_receivable_supervisor_note_work_item_id",
        "receivable_supervisor_note",
        ["work_item_id"],
        unique=False,
    )
    op.create_index(
        "ix_receivable_supervisor_note_author",
        "receivable_supervisor_note",
        ["author_bitrix_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_receivable_supervisor_note_visibility",
        "receivable_supervisor_note",
        ["visibility"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_receivable_supervisor_note_visibility",
        table_name="receivable_supervisor_note",
    )
    op.drop_index(
        "ix_receivable_supervisor_note_author",
        table_name="receivable_supervisor_note",
    )
    op.drop_index(
        "ix_receivable_supervisor_note_work_item_id",
        table_name="receivable_supervisor_note",
    )
    op.drop_table("receivable_supervisor_note")
