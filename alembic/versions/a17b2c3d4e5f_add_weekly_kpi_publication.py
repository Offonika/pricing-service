"""add weekly kpi publication tables

Revision ID: a17b2c3d4e5f
Revises: fe45ab67cd89
Create Date: 2026-04-07 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a17b2c3d4e5f"
down_revision: str | Sequence[str] | None = "fe45ab67cd89"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "weekly_kpi_report_snapshot",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("report_key", sa.String(length=255), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("week_end", sa.Date(), nullable=False),
        sa.Column("employee_key", sa.String(length=128), nullable=False),
        sa.Column("employee_name", sa.String(length=255), nullable=False),
        sa.Column("role_code", sa.String(length=128), nullable=True),
        sa.Column("position_code", sa.String(length=128), nullable=True),
        sa.Column("position_name", sa.String(length=255), nullable=True),
        sa.Column("bitrix_user_id", sa.String(length=64), nullable=True),
        sa.Column("bitrix_box_user_id", sa.String(length=64), nullable=True),
        sa.Column("lifecycle_status", sa.String(length=32), nullable=False),
        sa.Column("eligibility_status", sa.String(length=32), nullable=False),
        sa.Column("eligibility_reason", sa.Text(), nullable=True),
        sa.Column("artifact_status", sa.String(length=32), nullable=False),
        sa.Column("overall_signal", sa.String(length=32), nullable=True),
        sa.Column("summary_payload", sa.JSON(), nullable=True),
        sa.Column("source_as_of", sa.Date(), nullable=True),
        sa.Column("generated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("superseded_at", sa.DateTime(), nullable=True),
        sa.Column("artifact_path", sa.String(length=512), nullable=True),
        sa.Column("artifact_sha256", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "report_key",
            "revision",
            name="uq_weekly_kpi_report_snapshot_key_revision",
        ),
    )
    op.create_index(
        "ix_weekly_kpi_report_snapshot_week_end",
        "weekly_kpi_report_snapshot",
        ["week_end"],
        unique=False,
    )
    op.create_index(
        "ix_weekly_kpi_report_snapshot_lifecycle",
        "weekly_kpi_report_snapshot",
        ["week_end", "lifecycle_status", "eligibility_status"],
        unique=False,
    )
    op.create_index(
        "ix_weekly_kpi_report_snapshot_employee_key",
        "weekly_kpi_report_snapshot",
        ["employee_key"],
        unique=False,
    )
    op.create_index(
        "ix_weekly_kpi_report_snapshot_bitrix_user_id",
        "weekly_kpi_report_snapshot",
        ["bitrix_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_weekly_kpi_report_snapshot_artifact_status",
        "weekly_kpi_report_snapshot",
        ["artifact_status"],
        unique=False,
    )

    op.create_table(
        "weekly_kpi_report_metric_snapshot",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("report_id", sa.Integer(), nullable=False),
        sa.Column("metric_code", sa.String(length=128), nullable=False),
        sa.Column("metric_name", sa.String(length=255), nullable=False),
        sa.Column("unit", sa.String(length=32), nullable=True),
        sa.Column("fact_value", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("plan_value", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("achievement_pct", sa.Numeric(precision=10, scale=4), nullable=True),
        sa.Column("bonus_preview_amount", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("weight", sa.Numeric(precision=10, scale=4), nullable=True),
        sa.Column("previous_fact_value", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("delta_abs", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("delta_pct", sa.Numeric(precision=10, scale=4), nullable=True),
        sa.Column("signal", sa.String(length=32), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("source_system", sa.String(length=64), nullable=True),
        sa.Column("source_entity", sa.String(length=128), nullable=True),
        sa.Column("source_as_of", sa.Date(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["report_id"],
            ["weekly_kpi_report_snapshot.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "report_id",
            "metric_code",
            name="uq_weekly_kpi_report_metric_snapshot_report_metric",
        ),
    )
    op.create_index(
        "ix_weekly_kpi_report_metric_snapshot_report_id",
        "weekly_kpi_report_metric_snapshot",
        ["report_id"],
        unique=False,
    )
    op.create_index(
        "ix_weekly_kpi_report_metric_snapshot_signal",
        "weekly_kpi_report_metric_snapshot",
        ["signal"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_weekly_kpi_report_metric_snapshot_signal",
        table_name="weekly_kpi_report_metric_snapshot",
    )
    op.drop_index(
        "ix_weekly_kpi_report_metric_snapshot_report_id",
        table_name="weekly_kpi_report_metric_snapshot",
    )
    op.drop_table("weekly_kpi_report_metric_snapshot")

    op.drop_index(
        "ix_weekly_kpi_report_snapshot_artifact_status",
        table_name="weekly_kpi_report_snapshot",
    )
    op.drop_index(
        "ix_weekly_kpi_report_snapshot_bitrix_user_id",
        table_name="weekly_kpi_report_snapshot",
    )
    op.drop_index(
        "ix_weekly_kpi_report_snapshot_employee_key",
        table_name="weekly_kpi_report_snapshot",
    )
    op.drop_index(
        "ix_weekly_kpi_report_snapshot_lifecycle",
        table_name="weekly_kpi_report_snapshot",
    )
    op.drop_index(
        "ix_weekly_kpi_report_snapshot_week_end",
        table_name="weekly_kpi_report_snapshot",
    )
    op.drop_table("weekly_kpi_report_snapshot")
