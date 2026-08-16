"""add versioned display family registry

Revision ID: 0a8c2e4f6b7d
Revises: c3e5a7b9d1f2
Create Date: 2026-08-16 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0a8c2e4f6b7d"
down_revision = "c3e5a7b9d1f2"
branch_labels = None
depends_on = None


JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "display_family_registry_version",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("source_schema", sa.String(length=100), nullable=False),
        sa.Column("source_bundle_path", sa.String(length=512), nullable=False),
        sa.Column("inventory_checksum", sa.String(length=64), nullable=False),
        sa.Column("membership_checksum", sa.String(length=64), nullable=False),
        sa.Column("inventory_sha256", sa.String(length=64), nullable=False),
        sa.Column("inventory_csv_sha256", sa.String(length=64), nullable=False),
        sa.Column("report_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_quality_checksum", sa.String(length=64), nullable=False),
        sa.Column("expected_family_count", sa.Integer(), nullable=False),
        sa.Column("expected_member_count", sa.Integer(), nullable=False),
        sa.Column("actual_family_count", sa.Integer(), nullable=False),
        sa.Column("actual_member_count", sa.Integer(), nullable=False),
        sa.Column("source_manifest_json", JSON_TYPE, nullable=False),
        sa.Column("source_summary_json", JSON_TYPE, nullable=False),
        sa.Column("evidence_snapshot_json", JSON_TYPE, nullable=False),
        sa.Column("created_by", sa.String(length=160), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rolled_back_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('active', 'superseded', 'rolled_back')",
            name="ck_display_family_registry_version_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "inventory_checksum", name="uq_display_family_registry_inventory_checksum"
        ),
        sa.UniqueConstraint("version_number", name="uq_display_family_registry_version_number"),
    )
    op.create_index(
        "ix_display_family_registry_version_effective_from",
        "display_family_registry_version",
        ["effective_from"],
        unique=False,
    )
    op.create_index(
        "ix_display_family_registry_version_status",
        "display_family_registry_version",
        ["status"],
        unique=False,
    )
    op.create_index(
        "uq_display_family_registry_single_active",
        "display_family_registry_version",
        ["status"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
        sqlite_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "display_family",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("registry_version_id", sa.Integer(), nullable=False),
        sa.Column("family_key", sa.String(length=80), nullable=False),
        sa.Column("member_count", sa.Integer(), nullable=False),
        sa.Column("is_singleton", sa.Boolean(), nullable=False),
        sa.Column("total_current_stock_qty", sa.Integer(), nullable=False),
        sa.Column("review_member_count", sa.Integer(), nullable=False),
        sa.Column("matching_review_member_count", sa.Integer(), nullable=False),
        sa.Column("quality_unknown_member_count", sa.Integer(), nullable=False),
        sa.Column("construction_unknown_member_count", sa.Integer(), nullable=False),
        sa.Column("phone_model_ids_json", JSON_TYPE, nullable=False),
        sa.Column("phone_models_json", JSON_TYPE, nullable=False),
        sa.Column("physical_model_signatures_json", JSON_TYPE, nullable=False),
        sa.Column("segment_ids_json", JSON_TYPE, nullable=False),
        sa.Column("warning_codes_json", JSON_TYPE, nullable=False),
        sa.Column("note_codes_json", JSON_TYPE, nullable=False),
        sa.Column("evidence_snapshot_json", JSON_TYPE, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["registry_version_id"],
            ["display_family_registry_version.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "registry_version_id", "family_key", name="uq_display_family_version_key"
        ),
    )
    op.create_index("ix_display_family_family_key", "display_family", ["family_key"])
    op.create_index(
        "ix_display_family_registry_version_id",
        "display_family",
        ["registry_version_id"],
    )
    op.create_index(
        "ix_display_family_version_review",
        "display_family",
        ["registry_version_id", "review_member_count"],
    )
    op.create_index(
        "ix_display_family_version_singleton",
        "display_family",
        ["registry_version_id", "is_singleton"],
    )

    op.create_table(
        "display_family_member",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("registry_version_id", sa.Integer(), nullable=False),
        sa.Column("family_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("segment_id", sa.String(length=160), nullable=False),
        sa.Column("proposal_status", sa.String(length=64), nullable=False),
        sa.Column("quality_segment", sa.String(length=64), nullable=False),
        sa.Column("construction_segment", sa.String(length=64), nullable=False),
        sa.Column("requires_manual_review", sa.Boolean(), nullable=False),
        sa.Column("current_stock_qty", sa.Integer(), nullable=False),
        sa.Column("warning_codes_json", JSON_TYPE, nullable=False),
        sa.Column("note_codes_json", JSON_TYPE, nullable=False),
        sa.Column("scope_reasons_json", JSON_TYPE, nullable=False),
        sa.Column("product_snapshot_json", JSON_TYPE, nullable=False),
        sa.Column("matching_evidence_json", JSON_TYPE, nullable=False),
        sa.Column("identity_evidence_json", JSON_TYPE, nullable=False),
        sa.Column("evidence_snapshot_json", JSON_TYPE, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["family_id"], ["display_family.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["product_id"], ["product.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["registry_version_id"],
            ["display_family_registry_version.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "registry_version_id",
            "product_id",
            name="uq_display_family_member_version_product",
        ),
    )
    op.create_index("ix_display_family_member_family_id", "display_family_member", ["family_id"])
    op.create_index(
        "ix_display_family_member_family_segment",
        "display_family_member",
        ["family_id", "segment_id"],
    )
    op.create_index("ix_display_family_member_product_id", "display_family_member", ["product_id"])
    op.create_index(
        "ix_display_family_member_registry_version_id",
        "display_family_member",
        ["registry_version_id"],
    )
    op.create_index("ix_display_family_member_segment_id", "display_family_member", ["segment_id"])

    op.create_table(
        "display_family_decision_event",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("registry_version_id", sa.Integer(), nullable=False),
        sa.Column("family_id", sa.Integer(), nullable=True),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("actor", sa.String(length=160), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("effective_at", sa.Date(), nullable=False),
        sa.Column("evidence_snapshot_json", JSON_TYPE, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["family_id"], ["display_family.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["product_id"], ["product.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["registry_version_id"],
            ["display_family_registry_version.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_display_family_decision_event_action", "display_family_decision_event", ["action"]
    )
    op.create_index(
        "ix_display_family_decision_event_family_id", "display_family_decision_event", ["family_id"]
    )
    op.create_index(
        "ix_display_family_decision_event_product_id",
        "display_family_decision_event",
        ["product_id"],
    )
    op.create_index(
        "ix_display_family_decision_event_registry_version_id",
        "display_family_decision_event",
        ["registry_version_id"],
    )
    op.create_index(
        "ix_display_family_event_family_created",
        "display_family_decision_event",
        ["family_id", "created_at"],
    )
    op.create_index(
        "ix_display_family_event_version_created",
        "display_family_decision_event",
        ["registry_version_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("display_family_decision_event")
    op.drop_table("display_family_member")
    op.drop_table("display_family")
    op.drop_table("display_family_registry_version")
