"""add customer price type read-only core

Revision ID: b9e5d7f3a012
Revises: a8d4c6e2f901
Create Date: 2026-07-18 18:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "b9e5d7f3a012"
down_revision = "a8d4c6e2f901"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "customer_price_type_profile",
        sa.Column("counterparty_ref", sa.String(length=64), nullable=False),
        sa.Column("counterparty_code", sa.String(length=64), nullable=True),
        sa.Column("counterparty_name", sa.String(length=500), nullable=True),
        sa.Column("department_ref", sa.String(length=64), nullable=True),
        sa.Column("department_name", sa.String(length=255), nullable=True),
        sa.Column("owner_ref", sa.String(length=64), nullable=True),
        sa.Column("owner_name", sa.String(length=255), nullable=True),
        sa.Column("is_service_card", sa.Boolean(), nullable=False),
        sa.Column("is_hygiene", sa.Boolean(), nullable=False),
        sa.Column("master_data_flags", sa.JSON(), nullable=False),
        sa.Column("latest_snapshot_id", sa.Integer(), nullable=True),
        sa.Column("open_case_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.CheckConstraint(
            "counterparty_ref = lower(counterparty_ref)",
            name="ck_customer_price_type_profile_ref_lower",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("counterparty_ref", name="uq_customer_price_type_profile_ref"),
    )
    op.create_index(
        "ix_customer_price_type_profile_department",
        "customer_price_type_profile",
        ["department_ref"],
        unique=False,
    )
    op.create_index(
        "ix_customer_price_type_profile_owner",
        "customer_price_type_profile",
        ["owner_ref"],
        unique=False,
    )
    op.create_table(
        "customer_price_type_run",
        sa.Column("run_key", sa.String(length=255), nullable=False),
        sa.Column("snapshot_month", sa.Date(), nullable=False),
        sa.Column("ruleset_version", sa.String(length=64), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("window_start", sa.Date(), nullable=False),
        sa.Column("window_end", sa.Date(), nullable=False),
        sa.Column("source_statuses", sa.JSON(), nullable=False),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("input_count", sa.Integer(), nullable=False),
        sa.Column("excluded_count", sa.Integer(), nullable=False),
        sa.Column("calculated_count", sa.Integer(), nullable=False),
        sa.Column("conflict_count", sa.Integer(), nullable=False),
        sa.Column("actionable_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.CheckConstraint(
            "status IN ('started','completed','partial','failed')",
            name="ck_customer_price_type_run_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_key", name="uq_customer_price_type_run_key"),
    )
    op.create_index(
        "ix_customer_price_type_run_month_status",
        "customer_price_type_run",
        ["snapshot_month", "status"],
        unique=False,
    )
    op.create_index(
        "ix_customer_price_type_run_fingerprint",
        "customer_price_type_run",
        ["source_fingerprint"],
        unique=False,
    )
    op.create_index(
        "ix_customer_price_type_run_started",
        "customer_price_type_run",
        ["started_at"],
        unique=False,
    )
    op.create_table(
        "customer_price_type_snapshot",
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("counterparty_ref", sa.String(length=64), nullable=False),
        sa.Column("snapshot_month", sa.Date(), nullable=False),
        sa.Column("ruleset_version", sa.String(length=64), nullable=False),
        sa.Column("current_price_type", sa.String(length=255), nullable=True),
        sa.Column("current_level", sa.String(length=32), nullable=True),
        sa.Column("price_type_variant", sa.String(length=32), nullable=True),
        sa.Column("contract_candidates", sa.JSON(), nullable=False),
        sa.Column("monthly_sales", sa.JSON(), nullable=False),
        sa.Column("total_3m", sa.Numeric(18, 2), nullable=False),
        sa.Column("last_month", sa.Numeric(18, 2), nullable=False),
        sa.Column("economics", sa.JSON(), nullable=False),
        sa.Column("payments", sa.JSON(), nullable=False),
        sa.Column("returns", sa.JSON(), nullable=False),
        sa.Column("history", sa.JSON(), nullable=False),
        sa.Column("source_status", sa.String(length=32), nullable=False),
        sa.Column("source_statuses", sa.JSON(), nullable=False),
        sa.Column("conflicts", sa.JSON(), nullable=False),
        sa.Column("stop_factors", sa.JSON(), nullable=False),
        sa.Column("system_recommendation", sa.String(length=128), nullable=False),
        sa.Column("recommended_price_type", sa.String(length=255), nullable=True),
        sa.Column("recommendation_reason", sa.Text(), nullable=False),
        sa.Column("action_required", sa.Boolean(), nullable=False),
        sa.Column("case_type", sa.String(length=64), nullable=True),
        sa.Column("review_type", sa.String(length=64), nullable=True),
        sa.Column("reasons", sa.JSON(), nullable=False),
        sa.Column("snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.CheckConstraint(
            "source_status IN ('ready','partial','conflict','excluded')",
            name="ck_customer_price_type_snapshot_source_status",
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"], ["customer_price_type_profile.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["run_id"], ["customer_price_type_run.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id", "profile_id", name="uq_customer_price_type_snapshot_run_profile"
        ),
    )
    op.create_index(
        "ix_customer_price_type_snapshot_profile_month",
        "customer_price_type_snapshot",
        ["profile_id", "snapshot_month"],
        unique=False,
    )
    op.create_index(
        "ix_customer_price_type_snapshot_month_action",
        "customer_price_type_snapshot",
        ["snapshot_month", "action_required"],
        unique=False,
    )
    op.create_index(
        "ix_customer_price_type_snapshot_hash",
        "customer_price_type_snapshot",
        ["snapshot_hash"],
        unique=False,
    )
    op.create_index(
        "ix_customer_price_type_snapshot_source",
        "customer_price_type_snapshot",
        ["source_status"],
        unique=False,
    )
    op.create_table(
        "customer_price_type_case",
        sa.Column("case_key", sa.String(length=96), nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("current_snapshot_id", sa.Integer(), nullable=False),
        sa.Column("snapshot_month", sa.Date(), nullable=False),
        sa.Column("ruleset_version", sa.String(length=64), nullable=False),
        sa.Column("case_type", sa.String(length=64), nullable=False),
        sa.Column("review_type", sa.String(length=64), nullable=True),
        sa.Column("reasons", sa.JSON(), nullable=False),
        sa.Column("stage", sa.String(length=32), nullable=False),
        sa.Column("owner_ref", sa.String(length=64), nullable=True),
        sa.Column("owner_name", sa.String(length=255), nullable=True),
        sa.Column("due_at", sa.DateTime(), nullable=True),
        sa.Column("department_ref", sa.String(length=64), nullable=True),
        sa.Column("department_name", sa.String(length=255), nullable=True),
        sa.Column("manager_action_completeness", sa.JSON(), nullable=False),
        sa.Column("system_recommendation", sa.String(length=128), nullable=False),
        sa.Column("recommended_price_type", sa.String(length=255), nullable=True),
        sa.Column("human_final_decision", sa.String(length=128), nullable=True),
        sa.Column("approval_status", sa.String(length=32), nullable=False),
        sa.Column("approver_ref", sa.String(length=64), nullable=True),
        sa.Column("approver_name", sa.String(length=255), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("approved_snapshot_hash", sa.String(length=64), nullable=True),
        sa.Column("bitrix_entity_id", sa.String(length=64), nullable=True),
        sa.Column("bitrix_sync_version", sa.Integer(), nullable=True),
        sa.Column("onec_export_status", sa.String(length=32), nullable=False),
        sa.Column("onec_readback_status", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.CheckConstraint(
            "stage IN ('NEW','MANAGER_WORK','ISOLATE','DATA_CHECK','SPECIAL_REVIEW',"
            "'DOWNGRADE_APPROVAL','READY_FOR_1C','CLOSED_KEEP','CLOSED_CHANGED','ONEC_ERROR')",
            name="ck_customer_price_type_case_stage",
        ),
        sa.CheckConstraint(
            "case_type IN ('manager_work','isolate','recovery','data_check',"
            "'special_review','downgrade_approval')",
            name="ck_customer_price_type_case_type",
        ),
        sa.CheckConstraint(
            "approval_status IN ('not_requested','pending','approved','rejected','stale')",
            name="ck_customer_price_type_case_approval_status",
        ),
        sa.CheckConstraint(
            "onec_export_status IN ('not_ready','blocked','ready','exported','error')",
            name="ck_customer_price_type_case_onec_export_status",
        ),
        sa.CheckConstraint(
            "onec_readback_status IN ('not_requested','pending','confirmed','mismatch','error')",
            name="ck_customer_price_type_case_onec_readback_status",
        ),
        sa.CheckConstraint("version > 0", name="ck_customer_price_type_case_version"),
        sa.ForeignKeyConstraint(
            ["current_snapshot_id"],
            ["customer_price_type_snapshot.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"], ["customer_price_type_profile.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("case_key", name="uq_customer_price_type_case_key"),
        sa.UniqueConstraint(
            "profile_id", "snapshot_month", name="uq_customer_price_type_case_profile_month"
        ),
    )
    op.create_index(
        "ix_customer_price_type_case_month_stage",
        "customer_price_type_case",
        ["snapshot_month", "stage"],
        unique=False,
    )
    op.create_index(
        "ix_customer_price_type_case_worklist",
        "customer_price_type_case",
        ["snapshot_month", "case_type", "stage"],
        unique=False,
    )
    op.create_index(
        "ix_customer_price_type_case_department",
        "customer_price_type_case",
        ["department_ref", "stage"],
        unique=False,
    )
    op.create_index(
        "ix_customer_price_type_case_owner",
        "customer_price_type_case",
        ["owner_ref", "stage"],
        unique=False,
    )
    op.create_index(
        "ix_customer_price_type_case_review",
        "customer_price_type_case",
        ["review_type", "stage"],
        unique=False,
    )
    op.create_table(
        "customer_price_type_case_event",
        sa.Column("case_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("event_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("actor", sa.String(length=255), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("before_status", sa.String(length=64), nullable=True),
        sa.Column("after_status", sa.String(length=64), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.CheckConstraint(
            "source IN ('calculation','app','bitrix','onec','system')",
            name="ck_customer_price_type_case_event_source",
        ),
        sa.ForeignKeyConstraint(["case_id"], ["customer_price_type_case.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "case_id", "idempotency_key", name="uq_customer_price_type_case_event_key"
        ),
    )
    op.create_index(
        "ix_customer_price_type_case_event_case_at",
        "customer_price_type_case_event",
        ["case_id", "event_at"],
        unique=False,
    )
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("customer_price_type_profile") as batch_op:
            batch_op.create_foreign_key(
                "fk_customer_price_type_profile_latest_snapshot",
                "customer_price_type_snapshot",
                ["latest_snapshot_id"],
                ["id"],
                ondelete="SET NULL",
            )
            batch_op.create_foreign_key(
                "fk_customer_price_type_profile_open_case",
                "customer_price_type_case",
                ["open_case_id"],
                ["id"],
                ondelete="SET NULL",
            )
    else:
        op.create_foreign_key(
            "fk_customer_price_type_profile_latest_snapshot",
            "customer_price_type_profile",
            "customer_price_type_snapshot",
            ["latest_snapshot_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_foreign_key(
            "fk_customer_price_type_profile_open_case",
            "customer_price_type_profile",
            "customer_price_type_case",
            ["open_case_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("customer_price_type_profile") as batch_op:
            batch_op.drop_constraint("fk_customer_price_type_profile_open_case", type_="foreignkey")
            batch_op.drop_constraint(
                "fk_customer_price_type_profile_latest_snapshot", type_="foreignkey"
            )
    else:
        op.drop_constraint(
            "fk_customer_price_type_profile_open_case",
            "customer_price_type_profile",
            type_="foreignkey",
        )
        op.drop_constraint(
            "fk_customer_price_type_profile_latest_snapshot",
            "customer_price_type_profile",
            type_="foreignkey",
        )
    op.drop_index(
        "ix_customer_price_type_case_event_case_at",
        table_name="customer_price_type_case_event",
    )
    op.drop_table("customer_price_type_case_event")
    op.drop_index("ix_customer_price_type_case_worklist", table_name="customer_price_type_case")
    op.drop_index("ix_customer_price_type_case_review", table_name="customer_price_type_case")
    op.drop_index("ix_customer_price_type_case_owner", table_name="customer_price_type_case")
    op.drop_index("ix_customer_price_type_case_department", table_name="customer_price_type_case")
    op.drop_index("ix_customer_price_type_case_month_stage", table_name="customer_price_type_case")
    op.drop_table("customer_price_type_case")
    op.drop_index(
        "ix_customer_price_type_snapshot_source",
        table_name="customer_price_type_snapshot",
    )
    op.drop_index("ix_customer_price_type_snapshot_hash", table_name="customer_price_type_snapshot")
    op.drop_index(
        "ix_customer_price_type_snapshot_month_action",
        table_name="customer_price_type_snapshot",
    )
    op.drop_index(
        "ix_customer_price_type_snapshot_profile_month",
        table_name="customer_price_type_snapshot",
    )
    op.drop_table("customer_price_type_snapshot")
    op.drop_index("ix_customer_price_type_run_started", table_name="customer_price_type_run")
    op.drop_index("ix_customer_price_type_run_fingerprint", table_name="customer_price_type_run")
    op.drop_index("ix_customer_price_type_run_month_status", table_name="customer_price_type_run")
    op.drop_table("customer_price_type_run")
    op.drop_index("ix_customer_price_type_profile_owner", table_name="customer_price_type_profile")
    op.drop_index(
        "ix_customer_price_type_profile_department", table_name="customer_price_type_profile"
    )
    op.drop_table("customer_price_type_profile")
