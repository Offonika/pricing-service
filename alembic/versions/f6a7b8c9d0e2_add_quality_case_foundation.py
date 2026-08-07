"""add quality case foundation

Revision ID: f6a7b8c9d0e2
Revises: d5e6f7a8b9c1
Create Date: 2026-08-03 13:45:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "f6a7b8c9d0e2"
down_revision = "d5e6f7a8b9c1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "quality_case",
        sa.Column("external_id", sa.String(length=96), nullable=False),
        sa.Column("source_return_ref", sa.String(length=64), nullable=False),
        sa.Column("source_return_number", sa.String(length=64), nullable=True),
        sa.Column("source_return_line_key", sa.String(length=160), nullable=False),
        sa.Column("return_at", sa.DateTime(), nullable=False),
        sa.Column("nomenclature_ref", sa.String(length=64), nullable=True),
        sa.Column("nomenclature_code", sa.String(length=64), nullable=False),
        sa.Column("nomenclature_name", sa.String(length=1000), nullable=True),
        sa.Column("quantity", sa.Numeric(18, 3), nullable=False),
        sa.Column("store_external_id", sa.String(length=64), nullable=True),
        sa.Column("store_name", sa.String(length=255), nullable=True),
        sa.Column("preliminary_quality", sa.String(length=64), nullable=True),
        sa.Column("preliminary_reason_code", sa.String(length=64), nullable=True),
        sa.Column("current_status", sa.String(length=32), nullable=False),
        sa.Column("owner_external_id", sa.String(length=64), nullable=True),
        sa.Column("due_at", sa.DateTime(), nullable=True),
        sa.Column("final_decision_code", sa.String(length=64), nullable=True),
        sa.Column("disposition_code", sa.String(length=64), nullable=True),
        sa.Column("decision_comment", sa.String(length=2000), nullable=True),
        sa.Column("decision_author_external_id", sa.String(length=64), nullable=True),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("onec_quality_correction_ref", sa.String(length=64), nullable=True),
        sa.Column(
            "counts_as_confirmed_product_defect",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("external_id", name="uq_quality_case_external_id"),
        sa.UniqueConstraint(
            "source_return_line_key", name="uq_quality_case_source_return_line_key"
        ),
    )
    op.create_index("ix_quality_case_status_due_at", "quality_case", ["current_status", "due_at"])
    op.create_index(
        "ix_quality_case_nomenclature_return_at",
        "quality_case",
        ["nomenclature_code", "return_at"],
    )
    op.create_index(
        "ix_quality_case_confirmed_product_defect",
        "quality_case",
        ["counts_as_confirmed_product_defect"],
    )
    op.create_table(
        "quality_case_event",
        sa.Column("quality_case_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("event_at", sa.DateTime(), nullable=False),
        sa.Column("actor_external_id", sa.String(length=64), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("comment", sa.String(length=2000), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.ForeignKeyConstraint(["quality_case_id"], ["quality_case.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_quality_case_event_idempotency_key"),
    )
    op.create_index(
        "ix_quality_case_event_case_event_at",
        "quality_case_event",
        ["quality_case_id", "event_at"],
    )
    op.create_index("ix_quality_case_event_type", "quality_case_event", ["event_type"])


def downgrade() -> None:
    op.drop_index("ix_quality_case_event_type", table_name="quality_case_event")
    op.drop_index("ix_quality_case_event_case_event_at", table_name="quality_case_event")
    op.drop_table("quality_case_event")
    op.drop_index("ix_quality_case_confirmed_product_defect", table_name="quality_case")
    op.drop_index("ix_quality_case_nomenclature_return_at", table_name="quality_case")
    op.drop_index("ix_quality_case_status_due_at", table_name="quality_case")
    op.drop_table("quality_case")
