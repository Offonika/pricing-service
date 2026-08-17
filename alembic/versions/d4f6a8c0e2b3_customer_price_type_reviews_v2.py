"""add independent customer price-type reviews and external action outbox

Revision ID: d4f6a8c0e2b3
Revises: c3e5a7b9d1f2
Create Date: 2026-08-12 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "d4f6a8c0e2b3"
down_revision = "c3e5a7b9d1f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("customer_price_type_case") as batch_op:
        batch_op.drop_constraint("ck_customer_price_type_case_stage", type_="check")
        batch_op.drop_constraint("ck_customer_price_type_case_type", type_="check")

    op.execute(sa.text("""
            UPDATE customer_price_type_case
            SET stage = CASE stage
                WHEN 'NEW' THEN 'NEW_SNAPSHOT'
                WHEN 'MANAGER_WORK' THEN 'RETENTION_WORK'
                WHEN 'ISOLATE' THEN 'ISOLATE_1M'
                WHEN 'SPECIAL_REVIEW' THEN 'QUALITY_CHECK'
                WHEN 'ONEC_ERROR' THEN 'READY_FOR_1C'
                ELSE stage
            END
            """))

    with op.batch_alter_table("customer_price_type_case") as batch_op:
        batch_op.create_check_constraint(
            "ck_customer_price_type_case_stage",
            "stage IN ('NEW_SNAPSHOT','PRECLOSE_SIGNAL','RETENTION_WORK','ISOLATE_1M',"
            "'RECOVERY_CONTROL','QUALITY_CHECK','CREDIT_ECONOMICS_CHECK','DATA_CHECK',"
            "'UPGRADE_APPROVAL','DOWNGRADE_APPROVAL','READY_FOR_1C','CLOSED_KEEP',"
            "'CLOSED_CHANGED')",
        )
        batch_op.create_check_constraint(
            "ck_customer_price_type_case_type",
            "case_type IN ('manager_work','isolate','recovery','data_check',"
            "'special_review','upgrade_approval','downgrade_approval')",
        )

    op.create_table(
        "customer_price_type_review",
        sa.Column("snapshot_id", sa.Integer(), nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("case_id", sa.Integer(), nullable=True),
        sa.Column("review_kind", sa.String(length=32), nullable=False),
        sa.Column("system_value", sa.String(length=255), nullable=True),
        sa.Column("final_value", sa.String(length=255), nullable=True),
        sa.Column("result", sa.String(length=32), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("decision_mode", sa.String(length=16), nullable=False),
        sa.Column("reviewed_by", sa.String(length=255), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.CheckConstraint(
            "decision_mode IN ('test','live')",
            name="ck_customer_price_type_review_decision_mode",
        ),
        sa.CheckConstraint(
            "result IN ('confirm','correct','no_action','data_issue')",
            name="ck_customer_price_type_review_result",
        ),
        sa.CheckConstraint(
            "review_kind IN ('price_type','client_action')",
            name="ck_customer_price_type_review_kind",
        ),
        sa.CheckConstraint("version > 0", name="ck_customer_price_type_review_version"),
        sa.ForeignKeyConstraint(["case_id"], ["customer_price_type_case.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["profile_id"], ["customer_price_type_profile.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"], ["customer_price_type_snapshot.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "snapshot_id",
            "review_kind",
            name="uq_customer_price_type_review_snapshot_kind",
        ),
    )
    op.create_index(
        "ix_customer_price_type_review_kind_at",
        "customer_price_type_review",
        ["review_kind", "reviewed_at"],
        unique=False,
    )
    op.create_index(
        "ix_customer_price_type_review_profile",
        "customer_price_type_review",
        ["profile_id", "review_kind"],
        unique=False,
    )

    op.create_table(
        "customer_price_type_external_action",
        sa.Column("review_id", sa.Integer(), nullable=False),
        sa.Column("snapshot_id", sa.Integer(), nullable=False),
        sa.Column("case_id", sa.Integer(), nullable=True),
        sa.Column("action_kind", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("execution_allowed_at_decision", sa.Boolean(), nullable=False),
        sa.Column("snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("cancelled_by", sa.String(length=255), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(), nullable=True),
        sa.Column("cancel_comment", sa.Text(), nullable=True),
        sa.Column("technical_message", sa.Text(), nullable=True),
        sa.Column("external_ref", sa.String(length=255), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.CheckConstraint(
            "action_kind IN ('onec_change','bitrix_case')",
            name="ck_customer_price_type_external_action_kind",
        ),
        sa.CheckConstraint(
            "status IN ('held','pending','preflight','ready_to_apply','applying','applied',"
            "'cancelled','technical_review')",
            name="ck_customer_price_type_external_action_status",
        ),
        sa.CheckConstraint("version > 0", name="ck_customer_price_type_external_action_version"),
        sa.ForeignKeyConstraint(["case_id"], ["customer_price_type_case.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["review_id"], ["customer_price_type_review.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"], ["customer_price_type_snapshot.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "review_id",
            "action_kind",
            name="uq_customer_price_type_external_action_review_kind",
        ),
        sa.UniqueConstraint("idempotency_key", name="uq_customer_price_type_external_action_key"),
    )
    op.create_index(
        "ix_customer_price_type_external_action_case",
        "customer_price_type_external_action",
        ["case_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_customer_price_type_external_action_work",
        "customer_price_type_external_action",
        ["action_kind", "status", "created_at"],
        unique=False,
    )

    op.create_table(
        "customer_price_type_onec_contract_action",
        sa.Column("external_action_id", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("contract_ref", sa.String(length=64), nullable=False),
        sa.Column("contract_name", sa.String(length=500), nullable=True),
        sa.Column("expected_price_type", sa.String(length=255), nullable=False),
        sa.Column("target_price_type", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("actual_price_type", sa.String(length=255), nullable=True),
        sa.Column("result_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.CheckConstraint(
            "status IN ('held','pending','ready','applying','applied','cancelled',"
            "'technical_review')",
            name="ck_customer_price_type_onec_contract_action_status",
        ),
        sa.ForeignKeyConstraint(
            ["external_action_id"],
            ["customer_price_type_external_action.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "external_action_id",
            "contract_ref",
            name="uq_customer_price_type_onec_action_contract",
        ),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_customer_price_type_onec_contract_action_key"
        ),
    )
    op.create_index(
        "ix_customer_price_type_onec_contract_action_work",
        "customer_price_type_onec_contract_action",
        ["status", "external_action_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_customer_price_type_onec_contract_action_work",
        table_name="customer_price_type_onec_contract_action",
    )
    op.drop_table("customer_price_type_onec_contract_action")
    op.drop_index(
        "ix_customer_price_type_external_action_work",
        table_name="customer_price_type_external_action",
    )
    op.drop_index(
        "ix_customer_price_type_external_action_case",
        table_name="customer_price_type_external_action",
    )
    op.drop_table("customer_price_type_external_action")
    op.drop_index("ix_customer_price_type_review_profile", table_name="customer_price_type_review")
    op.drop_index("ix_customer_price_type_review_kind_at", table_name="customer_price_type_review")
    op.drop_table("customer_price_type_review")

    with op.batch_alter_table("customer_price_type_case") as batch_op:
        batch_op.drop_constraint("ck_customer_price_type_case_stage", type_="check")
        batch_op.drop_constraint("ck_customer_price_type_case_type", type_="check")
    op.execute(sa.text("""
            UPDATE customer_price_type_case
            SET stage = CASE stage
                WHEN 'NEW_SNAPSHOT' THEN 'NEW'
                WHEN 'RETENTION_WORK' THEN 'MANAGER_WORK'
                WHEN 'ISOLATE_1M' THEN 'ISOLATE'
                WHEN 'QUALITY_CHECK' THEN 'SPECIAL_REVIEW'
                WHEN 'PRECLOSE_SIGNAL' THEN 'NEW'
                WHEN 'RECOVERY_CONTROL' THEN 'NEW'
                WHEN 'CREDIT_ECONOMICS_CHECK' THEN 'SPECIAL_REVIEW'
                WHEN 'UPGRADE_APPROVAL' THEN 'NEW'
                ELSE stage
            END
            """))
    with op.batch_alter_table("customer_price_type_case") as batch_op:
        batch_op.create_check_constraint(
            "ck_customer_price_type_case_stage",
            "stage IN ('NEW','MANAGER_WORK','ISOLATE','DATA_CHECK','SPECIAL_REVIEW',"
            "'DOWNGRADE_APPROVAL','READY_FOR_1C','CLOSED_KEEP','CLOSED_CHANGED','ONEC_ERROR')",
        )
        batch_op.create_check_constraint(
            "ck_customer_price_type_case_type",
            "case_type IN ('manager_work','isolate','recovery','data_check',"
            "'special_review','downgrade_approval')",
        )
