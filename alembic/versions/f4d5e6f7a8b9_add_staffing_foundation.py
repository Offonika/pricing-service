"""add staffing foundation

Revision ID: f4d5e6f7a8b9
Revises: f2b3c4d5e6f7
Create Date: 2026-03-20 17:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f4d5e6f7a8b9"
down_revision: str | Sequence[str] | None = "f2b3c4d5e6f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "staff_member",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("external_ref", sa.String(length=64), nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=False),
        sa.Column("role_code", sa.String(length=64), nullable=True),
        sa.Column("role_name", sa.String(length=255), nullable=True),
        sa.Column("department_ref", sa.String(length=64), nullable=True),
        sa.Column("department_name", sa.String(length=255), nullable=True),
        sa.Column("store_ref", sa.String(length=64), nullable=True),
        sa.Column("store_name", sa.String(length=255), nullable=True),
        sa.Column("employment_status", sa.String(length=32), nullable=False),
        sa.Column("hire_date", sa.Date(), nullable=True),
        sa.Column("termination_date", sa.Date(), nullable=True),
        sa.Column("manager_ref", sa.String(length=64), nullable=True),
        sa.Column("manager_name", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "external_ref", name="uq_staff_member_source_external_ref"),
    )
    op.create_index(
        "ix_staff_member_store_status",
        "staff_member",
        ["store_ref", "employment_status"],
        unique=False,
    )
    op.create_index(
        "ix_staff_member_department_ref",
        "staff_member",
        ["department_ref"],
        unique=False,
    )

    op.create_table(
        "store_shift_plan",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("business_key", sa.String(length=64), nullable=False),
        sa.Column("external_shift_ref", sa.String(length=64), nullable=False),
        sa.Column("slot_no", sa.Integer(), nullable=False),
        sa.Column("shift_date", sa.Date(), nullable=False),
        sa.Column("shift_code", sa.String(length=32), nullable=False),
        sa.Column("store_ref", sa.String(length=64), nullable=False),
        sa.Column("store_name", sa.String(length=255), nullable=True),
        sa.Column("role_code", sa.String(length=64), nullable=True),
        sa.Column("role_name", sa.String(length=255), nullable=True),
        sa.Column("planned_start_at", sa.DateTime(), nullable=True),
        sa.Column("planned_end_at", sa.DateTime(), nullable=True),
        sa.Column("staff_ref", sa.String(length=64), nullable=True),
        sa.Column("staff_name", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("business_key", name="uq_store_shift_plan_business_key"),
    )
    op.create_index(
        "ix_store_shift_plan_shift_date",
        "store_shift_plan",
        ["shift_date"],
        unique=False,
    )
    op.create_index(
        "ix_store_shift_plan_store_shift",
        "store_shift_plan",
        ["store_ref", "shift_code"],
        unique=False,
    )

    op.create_table(
        "store_shift_fact",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("business_key", sa.String(length=64), nullable=False),
        sa.Column("external_shift_ref", sa.String(length=64), nullable=False),
        sa.Column("slot_no", sa.Integer(), nullable=False),
        sa.Column("shift_date", sa.Date(), nullable=False),
        sa.Column("shift_code", sa.String(length=32), nullable=False),
        sa.Column("store_ref", sa.String(length=64), nullable=False),
        sa.Column("store_name", sa.String(length=255), nullable=True),
        sa.Column("role_code", sa.String(length=64), nullable=True),
        sa.Column("role_name", sa.String(length=255), nullable=True),
        sa.Column("staff_ref", sa.String(length=64), nullable=True),
        sa.Column("staff_name", sa.String(length=255), nullable=True),
        sa.Column("attendance_status", sa.String(length=32), nullable=False),
        sa.Column("actual_start_at", sa.DateTime(), nullable=True),
        sa.Column("actual_end_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("business_key", name="uq_store_shift_fact_business_key"),
    )
    op.create_index(
        "ix_store_shift_fact_shift_date",
        "store_shift_fact",
        ["shift_date"],
        unique=False,
    )
    op.create_index(
        "ix_store_shift_fact_store_shift",
        "store_shift_fact",
        ["store_ref", "shift_code"],
        unique=False,
    )

    op.create_table(
        "staffing_snapshot",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("store_ref", sa.String(length=64), nullable=False),
        sa.Column("store_name", sa.String(length=255), nullable=True),
        sa.Column("shift_code", sa.String(length=32), nullable=False),
        sa.Column("planned_count", sa.Integer(), nullable=False),
        sa.Column("assigned_count", sa.Integer(), nullable=False),
        sa.Column("confirmed_count", sa.Integer(), nullable=False),
        sa.Column("no_show_count", sa.Integer(), nullable=False),
        sa.Column("deficit_count", sa.Integer(), nullable=False),
        sa.Column("fill_rate", sa.Numeric(precision=8, scale=4), nullable=False),
        sa.Column("criticality", sa.String(length=16), nullable=False),
        sa.Column("deficit_role_counts", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "snapshot_date",
            "store_ref",
            "shift_code",
            name="uq_staffing_snapshot_date_store_shift",
        ),
    )
    op.create_index(
        "ix_staffing_snapshot_snapshot_date",
        "staffing_snapshot",
        ["snapshot_date"],
        unique=False,
    )
    op.create_index(
        "ix_staffing_snapshot_store_ref",
        "staffing_snapshot",
        ["store_ref"],
        unique=False,
    )
    op.create_index(
        "ix_staffing_snapshot_criticality",
        "staffing_snapshot",
        ["criticality"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_staffing_snapshot_criticality", table_name="staffing_snapshot")
    op.drop_index("ix_staffing_snapshot_store_ref", table_name="staffing_snapshot")
    op.drop_index("ix_staffing_snapshot_snapshot_date", table_name="staffing_snapshot")
    op.drop_table("staffing_snapshot")

    op.drop_index("ix_store_shift_fact_store_shift", table_name="store_shift_fact")
    op.drop_index("ix_store_shift_fact_shift_date", table_name="store_shift_fact")
    op.drop_table("store_shift_fact")

    op.drop_index("ix_store_shift_plan_store_shift", table_name="store_shift_plan")
    op.drop_index("ix_store_shift_plan_shift_date", table_name="store_shift_plan")
    op.drop_table("store_shift_plan")

    op.drop_index("ix_staff_member_department_ref", table_name="staff_member")
    op.drop_index("ix_staff_member_store_status", table_name="staff_member")
    op.drop_table("staff_member")
