"""add telephony user line snapshot

Revision ID: b1c2d3e4f5a6
Revises: a17b2c3d4e5f, ab67cd89ef01, ff56bc78de90
Create Date: 2026-04-18 15:20:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b1c2d3e4f5a6"
down_revision: str | Sequence[str] | None = (
    "a17b2c3d4e5f",
    "ab67cd89ef01",
    "ff56bc78de90",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "telephony_user_line_snapshot",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("mapping_source", sa.String(length=64), nullable=False),
        sa.Column("user_ref_hex", sa.String(length=64), nullable=False),
        sa.Column("user_name", sa.String(length=255), nullable=True),
        sa.Column("physical_person_ref_hex", sa.String(length=64), nullable=True),
        sa.Column("physical_person_name", sa.String(length=255), nullable=True),
        sa.Column("computer_name", sa.String(length=255), nullable=True),
        sa.Column("extension", sa.String(length=32), nullable=True),
        sa.Column("store_ref_hex", sa.String(length=64), nullable=True),
        sa.Column("store_code", sa.String(length=64), nullable=True),
        sa.Column("store_name", sa.String(length=255), nullable=True),
        sa.Column("department_ref_hex", sa.String(length=64), nullable=True),
        sa.Column("department_code", sa.String(length=64), nullable=True),
        sa.Column("department_name", sa.String(length=255), nullable=True),
        sa.Column("employment_status", sa.String(length=32), nullable=True),
        sa.Column("staff_store_ref", sa.String(length=64), nullable=True),
        sa.Column("staff_store_name", sa.String(length=255), nullable=True),
        sa.Column("staff_department_ref", sa.String(length=64), nullable=True),
        sa.Column("staff_department_name", sa.String(length=255), nullable=True),
        sa.Column("bitrix_user_id", sa.String(length=64), nullable=True),
        sa.Column("bitrix_full_name", sa.String(length=255), nullable=True),
        sa.Column("mdm_employee_code", sa.String(length=64), nullable=True),
        sa.Column("bitrix_status", sa.String(length=64), nullable=True),
        sa.Column("is_marked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("has_extension", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("has_bitrix", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "snapshot_date",
            "user_ref_hex",
            name="uq_telephony_user_line_snapshot_date_user",
        ),
    )
    op.create_index(
        "ix_telephony_user_line_snapshot_snapshot_date",
        "telephony_user_line_snapshot",
        ["snapshot_date"],
        unique=False,
    )
    op.create_index(
        "ix_telephony_user_line_snapshot_extension",
        "telephony_user_line_snapshot",
        ["extension"],
        unique=False,
    )
    op.create_index(
        "ix_telephony_user_line_snapshot_snapshot_extension",
        "telephony_user_line_snapshot",
        ["snapshot_date", "extension"],
        unique=False,
    )
    op.create_index(
        "ix_telephony_user_line_snapshot_snapshot_status",
        "telephony_user_line_snapshot",
        ["snapshot_date", "employment_status"],
        unique=False,
    )
    op.create_index(
        "ix_telephony_user_line_snapshot_bitrix_user_id",
        "telephony_user_line_snapshot",
        ["bitrix_user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_telephony_user_line_snapshot_bitrix_user_id",
        table_name="telephony_user_line_snapshot",
    )
    op.drop_index(
        "ix_telephony_user_line_snapshot_snapshot_status",
        table_name="telephony_user_line_snapshot",
    )
    op.drop_index(
        "ix_telephony_user_line_snapshot_snapshot_extension",
        table_name="telephony_user_line_snapshot",
    )
    op.drop_index(
        "ix_telephony_user_line_snapshot_extension",
        table_name="telephony_user_line_snapshot",
    )
    op.drop_index(
        "ix_telephony_user_line_snapshot_snapshot_date",
        table_name="telephony_user_line_snapshot",
    )
    op.drop_table("telephony_user_line_snapshot")
