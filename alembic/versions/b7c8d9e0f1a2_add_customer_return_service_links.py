"""add customer return service request and expertise links

Revision ID: b7c8d9e0f1a2
Revises: e9f1a3c5d7b9
Create Date: 2026-09-02
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b7c8d9e0f1a2"
down_revision: str | Sequence[str] | None = "c6d7e8f90123"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _positive_int(value: object) -> int | None:
    text = str(value or "").strip()
    if not text.isdigit():
        return None
    result = int(text)
    return result if result > 0 else None


def _backfill_service_request_ids() -> None:
    bind = op.get_bind()
    shipments = sa.table(
        "customer_return_shipment",
        sa.column("id", sa.Integer()),
        sa.column("bitrix_case_id", sa.String()),
        sa.column("site_ticket_id", sa.String()),
        sa.column("service_request_item_id", sa.Integer()),
    )
    cases = sa.table(
        "site_service_request_case",
        sa.column("source_ticket_id", sa.BigInteger()),
        sa.column("bitrix_item_id", sa.BigInteger()),
    )
    ticket_items = {
        int(ticket_id): int(item_id)
        for ticket_id, item_id in bind.execute(
            sa.select(cases.c.source_ticket_id, cases.c.bitrix_item_id).where(
                cases.c.bitrix_item_id.is_not(None)
            )
        )
        if ticket_id is not None and item_id is not None
    }
    for shipment_id, bitrix_case_id, site_ticket_id in bind.execute(
        sa.select(
            shipments.c.id,
            shipments.c.bitrix_case_id,
            shipments.c.site_ticket_id,
        )
    ):
        item_id = _positive_int(bitrix_case_id)
        if item_id is None:
            ticket_id = _positive_int(site_ticket_id)
            item_id = ticket_items.get(ticket_id) if ticket_id is not None else None
        if item_id is not None:
            bind.execute(
                sa.update(shipments)
                .where(shipments.c.id == shipment_id)
                .values(service_request_item_id=item_id)
            )


def upgrade() -> None:
    for name, column_type in (
        ("service_request_item_id", sa.Integer()),
        ("service_request_title", sa.String(255)),
        ("service_request_stage_id", sa.String(64)),
        ("service_request_stage_name", sa.String(255)),
        ("service_request_closed", sa.Boolean()),
        ("service_request_deal_id", sa.Integer()),
        ("service_request_order_ref", sa.String(64)),
        ("service_request_responsible_user_id", sa.Integer()),
        ("service_request_responsible_name", sa.String(255)),
        ("service_request_linked_at", sa.DateTime(timezone=True)),
        ("service_request_linked_by_user_id", sa.String(64)),
    ):
        op.add_column("customer_return_shipment", sa.Column(name, column_type))
    op.create_index(
        "ix_customer_return_shipment_service_request",
        "customer_return_shipment",
        ["service_request_item_id"],
    )

    op.add_column("expertise_case", sa.Column("service_request_item_id", sa.Integer()))
    op.add_column(
        "expertise_case",
        sa.Column("service_request_linked_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "expertise_case",
        sa.Column("service_request_linked_by_user_id", sa.String(64)),
    )
    op.create_index(
        "ix_expertise_case_service_request",
        "expertise_case",
        ["service_request_item_id"],
    )
    _backfill_service_request_ids()


def downgrade() -> None:
    op.drop_index("ix_expertise_case_service_request", table_name="expertise_case")
    for name in (
        "service_request_linked_by_user_id",
        "service_request_linked_at",
        "service_request_item_id",
    ):
        op.drop_column("expertise_case", name)

    op.drop_index(
        "ix_customer_return_shipment_service_request",
        table_name="customer_return_shipment",
    )
    for name in (
        "service_request_linked_by_user_id",
        "service_request_linked_at",
        "service_request_responsible_name",
        "service_request_responsible_user_id",
        "service_request_order_ref",
        "service_request_deal_id",
        "service_request_closed",
        "service_request_stage_name",
        "service_request_stage_id",
        "service_request_title",
        "service_request_item_id",
    ):
        op.drop_column("customer_return_shipment", name)
