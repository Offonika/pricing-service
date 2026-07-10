"""add assortment lifecycle classification cache

Revision ID: 1b2c3d4e5f60
Revises: 0a1b2c3d4e5f
Create Date: 2026-06-27 15:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "1b2c3d4e5f60"
down_revision = "0a1b2c3d4e5f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "assortment_lifecycle_classification_run",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_key", sa.String(length=128), nullable=False),
        sa.Column("folder", sa.String(length=255), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("source_status", sa.String(length=32), nullable=False),
        sa.Column("items_total", sa.Integer(), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("error_text", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_key", name="uq_assortment_lifecycle_run_key"),
    )
    op.create_index(
        "ix_assortment_lifecycle_run_started_at",
        "assortment_lifecycle_classification_run",
        ["started_at"],
        unique=False,
    )
    op.create_index(
        "ix_assortment_lifecycle_run_source_status",
        "assortment_lifecycle_classification_run",
        ["source_status"],
        unique=False,
    )

    op.create_table(
        "assortment_lifecycle_classification",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("nomenclature_code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("folder", sa.String(length=512), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("status_label", sa.String(length=128), nullable=False),
        sa.Column("recommended_status", sa.String(length=64), nullable=True),
        sa.Column("reason_codes", sa.JSON(), nullable=False),
        sa.Column("reason_text", sa.Text(), nullable=False),
        sa.Column("blockers", sa.JSON(), nullable=False),
        sa.Column("export_blockers", sa.JSON(), nullable=False),
        sa.Column("auto_order_allowed", sa.Boolean(), nullable=False),
        sa.Column("manual_review_required", sa.Boolean(), nullable=False),
        sa.Column("expensive_profile", sa.String(length=64), nullable=True),
        sa.Column("expensive_profile_label", sa.String(length=128), nullable=False),
        sa.Column("expensive_reason_codes", sa.JSON(), nullable=False),
        sa.Column("sales_point_warehouse_codes", sa.JSON(), nullable=False),
        sa.Column("manager_need_signals", sa.JSON(), nullable=False),
        sa.Column("source_record", sa.JSON(), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("classified_at", sa.DateTime(), nullable=False),
        sa.Column("last_run_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["last_run_id"],
            ["assortment_lifecycle_classification_run.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "nomenclature_code",
            name="uq_assortment_lifecycle_classification_code",
        ),
    )
    op.create_index(
        "ix_assortment_lifecycle_classification_status",
        "assortment_lifecycle_classification",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_assortment_lifecycle_classification_manual_review",
        "assortment_lifecycle_classification",
        ["manual_review_required"],
        unique=False,
    )
    op.create_index(
        "ix_assortment_lifecycle_classification_classified_at",
        "assortment_lifecycle_classification",
        ["classified_at"],
        unique=False,
    )
    op.create_index(
        "ix_assortment_lifecycle_classification_source_hash",
        "assortment_lifecycle_classification",
        ["source_hash"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_assortment_lifecycle_classification_source_hash",
        table_name="assortment_lifecycle_classification",
    )
    op.drop_index(
        "ix_assortment_lifecycle_classification_classified_at",
        table_name="assortment_lifecycle_classification",
    )
    op.drop_index(
        "ix_assortment_lifecycle_classification_manual_review",
        table_name="assortment_lifecycle_classification",
    )
    op.drop_index(
        "ix_assortment_lifecycle_classification_status",
        table_name="assortment_lifecycle_classification",
    )
    op.drop_table("assortment_lifecycle_classification")
    op.drop_index(
        "ix_assortment_lifecycle_run_source_status",
        table_name="assortment_lifecycle_classification_run",
    )
    op.drop_index(
        "ix_assortment_lifecycle_run_started_at",
        table_name="assortment_lifecycle_classification_run",
    )
    op.drop_table("assortment_lifecycle_classification_run")
