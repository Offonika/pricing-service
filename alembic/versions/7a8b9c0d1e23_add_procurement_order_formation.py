"""add procurement order formation

Revision ID: 7a8b9c0d1e23
Revises: 6f7a8b9c0d12
Create Date: 2026-07-10 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "7a8b9c0d1e23"
down_revision = "6f7a8b9c0d12"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "procurement_order_formation",
        sa.Column("stable_key", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("bitrix_entity_type_id", sa.Integer(), nullable=True),
        sa.Column("bitrix_item_id", sa.String(length=64), nullable=True),
        sa.Column("bitrix_category_id", sa.Integer(), nullable=True),
        sa.Column("bitrix_stage_id", sa.String(length=128), nullable=True),
        sa.Column("bitrix_item_url", sa.String(length=1000), nullable=True),
        sa.Column("supplier_ref", sa.String(length=64), nullable=True),
        sa.Column("supplier_code", sa.String(length=64), nullable=True),
        sa.Column("supplier_name", sa.String(length=500), nullable=False),
        sa.Column("contract_ref", sa.String(length=64), nullable=True),
        sa.Column("contract_code", sa.String(length=64), nullable=True),
        sa.Column("contract_name", sa.String(length=500), nullable=False),
        sa.Column("warehouse_ref", sa.String(length=64), nullable=True),
        sa.Column("warehouse_code", sa.String(length=64), nullable=True),
        sa.Column("warehouse_name", sa.String(length=500), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("procurement_contour", sa.String(length=64), nullable=False),
        sa.Column("route", sa.String(length=128), nullable=False),
        sa.Column("batch_id", sa.String(length=128), nullable=False),
        sa.Column("order_date", sa.Date(), nullable=False),
        sa.Column("responsible_bitrix_user_id", sa.String(length=64), nullable=True),
        sa.Column("responsible_name", sa.String(length=255), nullable=True),
        sa.Column("calculation_id", sa.String(length=160), nullable=False),
        sa.Column("source_run_id", sa.String(length=160), nullable=True),
        sa.Column("approved_version", sa.Integer(), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("approved_by_actor", sa.String(length=255), nullable=True),
        sa.Column("approved_by_bitrix_user_id", sa.String(length=64), nullable=True),
        sa.Column("approved_by_name", sa.String(length=255), nullable=True),
        sa.Column("onec_status", sa.String(length=32), nullable=False),
        sa.Column("onec_message_id", sa.String(length=160), nullable=True),
        sa.Column("onec_document_ref", sa.String(length=64), nullable=True),
        sa.Column("onec_document_number", sa.String(length=64), nullable=True),
        sa.Column("onec_document_date", sa.Date(), nullable=True),
        sa.Column("onec_error", sa.Text(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "bitrix_entity_type_id",
            "bitrix_item_id",
            name="uq_proc_order_formation_bitrix_item",
        ),
        sa.UniqueConstraint("stable_key", name="uq_proc_order_formation_stable_key"),
    )
    op.create_index("ix_proc_order_formation_status", "procurement_order_formation", ["status"])
    op.create_index(
        "ix_proc_order_formation_supplier",
        "procurement_order_formation",
        ["supplier_ref", "supplier_code"],
    )
    op.create_index(
        "ix_proc_order_formation_onec_status",
        "procurement_order_formation",
        ["onec_status"],
    )

    op.create_table(
        "procurement_order_formation_line",
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("stable_key", sa.String(length=255), nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("bitrix_product_id", sa.String(length=64), nullable=True),
        sa.Column("bitrix_product_xml_id", sa.String(length=64), nullable=False),
        sa.Column("nomenclature_ref", sa.String(length=64), nullable=False),
        sa.Column("nomenclature_code", sa.String(length=64), nullable=True),
        sa.Column("nomenclature_name", sa.String(length=1000), nullable=False),
        sa.Column("recommended_quantity", sa.Numeric(18, 3), nullable=False),
        sa.Column("final_quantity", sa.Numeric(18, 3), nullable=False),
        sa.Column("purchase_price", sa.Numeric(18, 4), nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("source_kind", sa.String(length=64), nullable=False),
        sa.Column("explicit_demand", sa.Boolean(), nullable=False),
        sa.Column("risk_level", sa.String(length=32), nullable=True),
        sa.Column("risk_codes", sa.JSON(), nullable=False),
        sa.Column("recommendation_reason", sa.Text(), nullable=True),
        sa.Column("blockers", sa.JSON(), nullable=False),
        sa.Column("assortment_status", sa.String(length=128), nullable=True),
        sa.Column("lifecycle_status", sa.String(length=128), nullable=True),
        sa.Column("quality", sa.String(length=128), nullable=True),
        sa.Column("procurement_profile", sa.String(length=128), nullable=True),
        sa.Column("manual_minimum", sa.Numeric(18, 3), nullable=True),
        sa.Column("removed", sa.Boolean(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.ForeignKeyConstraint(
            ["order_id"], ["procurement_order_formation.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_id", "line_number", name="uq_proc_order_line_order_number"),
        sa.UniqueConstraint("stable_key", name="uq_proc_order_line_stable_key"),
    )
    op.create_index(
        "ix_proc_order_line_order",
        "procurement_order_formation_line",
        ["order_id", "removed"],
    )
    op.create_index(
        "ix_proc_order_line_onec_ref",
        "procurement_order_formation_line",
        ["nomenclature_ref"],
    )
    op.create_index(
        "ix_proc_order_line_bitrix_product",
        "procurement_order_formation_line",
        ["bitrix_product_id"],
    )

    op.create_table(
        "procurement_classification_proposal",
        sa.Column("line_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("previous_status", sa.String(length=128), nullable=True),
        sa.Column("proposed_status", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("manual_minimum", sa.Numeric(18, 3), nullable=True),
        sa.Column("review_date", sa.Date(), nullable=True),
        sa.Column("blocks_order_line", sa.Boolean(), nullable=False),
        sa.Column("requested_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("requested_by_actor", sa.String(length=255), nullable=False),
        sa.Column("requested_by_bitrix_user_id", sa.String(length=64), nullable=False),
        sa.Column("requested_by_name", sa.String(length=255), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("approved_by_actor", sa.String(length=255), nullable=True),
        sa.Column("approved_by_bitrix_user_id", sa.String(length=64), nullable=True),
        sa.Column("approved_by_name", sa.String(length=255), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("onec_message_id", sa.String(length=160), nullable=True),
        sa.Column("onec_status", sa.String(length=32), nullable=False),
        sa.Column("onec_error", sa.Text(), nullable=True),
        sa.Column("bitrix_readback_value", sa.String(length=128), nullable=True),
        sa.Column("reflected_at", sa.DateTime(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.ForeignKeyConstraint(
            ["line_id"], ["procurement_order_formation_line.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_proc_class_proposal_idempotency"),
    )
    op.create_index(
        "ix_proc_class_proposal_line_status",
        "procurement_classification_proposal",
        ["line_id", "status"],
    )
    op.create_index(
        "ix_proc_class_proposal_message",
        "procurement_classification_proposal",
        ["onec_message_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_proc_class_proposal_message",
        table_name="procurement_classification_proposal",
    )
    op.drop_index(
        "ix_proc_class_proposal_line_status",
        table_name="procurement_classification_proposal",
    )
    op.drop_table("procurement_classification_proposal")
    op.drop_index(
        "ix_proc_order_line_bitrix_product",
        table_name="procurement_order_formation_line",
    )
    op.drop_index("ix_proc_order_line_onec_ref", table_name="procurement_order_formation_line")
    op.drop_index("ix_proc_order_line_order", table_name="procurement_order_formation_line")
    op.drop_table("procurement_order_formation_line")
    op.drop_index(
        "ix_proc_order_formation_onec_status",
        table_name="procurement_order_formation",
    )
    op.drop_index("ix_proc_order_formation_supplier", table_name="procurement_order_formation")
    op.drop_index("ix_proc_order_formation_status", table_name="procurement_order_formation")
    op.drop_table("procurement_order_formation")
