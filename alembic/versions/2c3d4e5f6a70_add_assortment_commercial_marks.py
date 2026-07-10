"""add assortment commercial marks

Revision ID: 2c3d4e5f6a70
Revises: 1b2c3d4e5f60
Create Date: 2026-06-27 15:20:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "2c3d4e5f6a70"
down_revision = "1b2c3d4e5f60"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "assortment_lifecycle_classification",
        sa.Column("commercial_marks", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )
    op.add_column(
        "assortment_lifecycle_classification",
        sa.Column(
            "commercial_mark_labels",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )
    op.add_column(
        "assortment_lifecycle_classification",
        sa.Column(
            "commercial_mark_blockers",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )
    op.add_column(
        "assortment_lifecycle_classification",
        sa.Column("exclusive_kind", sa.String(length=64), nullable=False, server_default=""),
    )
    op.add_column(
        "assortment_lifecycle_classification",
        sa.Column("exclusive_confidence", sa.String(length=64), nullable=False, server_default=""),
    )
    op.add_column(
        "assortment_lifecycle_classification",
        sa.Column("exclusive_checked_at", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "assortment_lifecycle_classification",
        sa.Column("exclusive_review_at", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "assortment_lifecycle_classification",
        sa.Column("exclusive_reason", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "assortment_lifecycle_classification",
        sa.Column(
            "exclusive_approved_by",
            sa.String(length=255),
            nullable=False,
            server_default="",
        ),
    )
    op.add_column(
        "assortment_lifecycle_classification",
        sa.Column(
            "exclusive_evidence_refs",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )
    op.add_column(
        "assortment_lifecycle_classification",
        sa.Column("exclusive_min_stock_qty", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_assortment_lifecycle_classification_exclusive_kind",
        "assortment_lifecycle_classification",
        ["exclusive_kind"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_assortment_lifecycle_classification_exclusive_kind",
        table_name="assortment_lifecycle_classification",
    )
    op.drop_column("assortment_lifecycle_classification", "exclusive_min_stock_qty")
    op.drop_column("assortment_lifecycle_classification", "exclusive_evidence_refs")
    op.drop_column("assortment_lifecycle_classification", "exclusive_approved_by")
    op.drop_column("assortment_lifecycle_classification", "exclusive_reason")
    op.drop_column("assortment_lifecycle_classification", "exclusive_review_at")
    op.drop_column("assortment_lifecycle_classification", "exclusive_checked_at")
    op.drop_column("assortment_lifecycle_classification", "exclusive_confidence")
    op.drop_column("assortment_lifecycle_classification", "exclusive_kind")
    op.drop_column("assortment_lifecycle_classification", "commercial_mark_blockers")
    op.drop_column("assortment_lifecycle_classification", "commercial_mark_labels")
    op.drop_column("assortment_lifecycle_classification", "commercial_marks")
