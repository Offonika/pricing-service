"""add customer return deal link

Revision ID: d8e0f2a4c6b9
Revises: c7d9e1f3a5b8
Create Date: 2026-09-01
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d8e0f2a4c6b9"
down_revision: str | Sequence[str] | None = "c7d9e1f3a5b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("customer_return_shipment", sa.Column("bitrix_deal_id", sa.Integer()))
    op.add_column("customer_return_shipment", sa.Column("bitrix_deal_title", sa.String(255)))
    op.add_column("customer_return_shipment", sa.Column("bitrix_order_ref", sa.String(64)))
    op.add_column("customer_return_shipment", sa.Column("bitrix_deal_stage_id", sa.String(64)))
    op.add_column("customer_return_shipment", sa.Column("bitrix_deal_stage_name", sa.String(255)))
    op.add_column("customer_return_shipment", sa.Column("bitrix_deal_closed", sa.Boolean()))
    op.add_column("customer_return_shipment", sa.Column("bitrix_contact_id", sa.Integer()))
    op.add_column("customer_return_shipment", sa.Column("bitrix_contact_name", sa.String(255)))
    op.add_column("customer_return_shipment", sa.Column("bitrix_company_id", sa.Integer()))
    op.add_column("customer_return_shipment", sa.Column("bitrix_company_name", sa.String(255)))
    op.add_column(
        "customer_return_shipment",
        sa.Column("bitrix_responsible_user_id", sa.Integer()),
    )
    op.add_column(
        "customer_return_shipment",
        sa.Column("bitrix_responsible_name", sa.String(255)),
    )
    op.add_column(
        "customer_return_shipment",
        sa.Column("bitrix_deal_linked_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "customer_return_shipment",
        sa.Column("bitrix_deal_linked_by_user_id", sa.String(64)),
    )
    op.create_index(
        "ix_customer_return_shipment_bitrix_deal",
        "customer_return_shipment",
        ["bitrix_deal_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_customer_return_shipment_bitrix_deal",
        table_name="customer_return_shipment",
    )
    for column_name in (
        "bitrix_deal_linked_by_user_id",
        "bitrix_deal_linked_at",
        "bitrix_responsible_name",
        "bitrix_responsible_user_id",
        "bitrix_company_name",
        "bitrix_company_id",
        "bitrix_contact_name",
        "bitrix_contact_id",
        "bitrix_deal_closed",
        "bitrix_deal_stage_name",
        "bitrix_deal_stage_id",
        "bitrix_order_ref",
        "bitrix_deal_title",
        "bitrix_deal_id",
    ):
        op.drop_column("customer_return_shipment", column_name)
