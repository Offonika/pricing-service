"""add logistics control contour

Revision ID: f0a1b2c3d4e5
Revises: e2f3a4b5c6d7
Create Date: 2026-05-22 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f0a1b2c3d4e5"
down_revision: str | Sequence[str] | None = "e2f3a4b5c6d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_postgresql() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    op.add_column(
        "logistics_transfer",
        sa.Column(
            "source_document_type",
            sa.String(length=32),
            server_default="transfer",
            nullable=True,
        ),
    )
    op.add_column(
        "logistics_transfer",
        sa.Column("lookup_code", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "logistics_transfer",
        sa.Column("document_target_warehouse_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "logistics_transfer",
        sa.Column("origin_order_external_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "logistics_transfer",
        sa.Column("site_order_number", sa.String(length=32), nullable=True),
    )
    op.execute(sa.text("""
            UPDATE logistics_transfer
            SET source_document_type = COALESCE(source_document_type, 'transfer'),
                lookup_code = COALESCE(lookup_code, barcode)
            """))
    if _is_postgresql():
        op.alter_column("logistics_transfer", "source_document_type", nullable=False)
        op.drop_constraint(
            "logistics_transfer_external_id_key",
            "logistics_transfer",
            type_="unique",
        )
        op.create_foreign_key(
            "fk_logistics_transfer_document_target_warehouse",
            "logistics_transfer",
            "logistics_warehouse",
            ["document_target_warehouse_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_unique_constraint(
            "uq_logistics_transfer_source_external",
            "logistics_transfer",
            ["source_document_type", "external_id"],
        )
    op.create_index(
        "ix_logistics_transfer_lookup_code",
        "logistics_transfer",
        ["lookup_code"],
        unique=False,
    )
    op.create_index(
        "ix_logistics_transfer_site_order_number",
        "logistics_transfer",
        ["site_order_number"],
        unique=False,
    )

    op.create_table(
        "logistics_route_run",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("external_id", sa.String(length=64), nullable=True),
        sa.Column("route_name", sa.String(length=255), nullable=False),
        sa.Column("planned_at", sa.DateTime(), nullable=True),
        sa.Column("driver_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="planned", nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["driver_id"], ["logistics_driver.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("external_id", name="uq_logistics_route_run_external_id"),
    )
    op.create_index(
        "ix_logistics_route_run_status_planned",
        "logistics_route_run",
        ["status", "planned_at"],
        unique=False,
    )

    op.add_column(
        "logistics_draft",
        sa.Column("route_run_id", sa.Integer(), nullable=True),
    )
    if _is_postgresql():
        op.create_foreign_key(
            "fk_logistics_draft_route_run",
            "logistics_draft",
            "logistics_route_run",
            ["route_run_id"],
            ["id"],
            ondelete="SET NULL",
        )

    op.create_table(
        "logistics_route_run_item",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("route_run_id", sa.Integer(), nullable=False),
        sa.Column("transfer_id", sa.Integer(), nullable=False),
        sa.Column("leg_sequence", sa.Integer(), nullable=True),
        sa.Column("dropoff_warehouse_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="planned", nullable=False),
        sa.Column("added_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["route_run_id"], ["logistics_route_run.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["transfer_id"], ["logistics_transfer.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["dropoff_warehouse_id"], ["logistics_warehouse.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("route_run_id", "transfer_id", name="uq_logistics_route_run_item_unit"),
    )
    op.create_index(
        "ix_logistics_route_run_item_transfer",
        "logistics_route_run_item",
        ["transfer_id"],
        unique=False,
    )
    op.create_index(
        "ix_logistics_route_run_item_dropoff",
        "logistics_route_run_item",
        ["dropoff_warehouse_id"],
        unique=False,
    )

    op.create_table(
        "logistics_manual_review",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("review_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="open", nullable=False),
        sa.Column("source_document_type", sa.String(length=32), nullable=True),
        sa.Column("source_external_id", sa.String(length=64), nullable=True),
        sa.Column("transfer_id", sa.Integer(), nullable=True),
        sa.Column("reason", sa.String(length=1000), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("resolved_by_user_id", sa.Integer(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["transfer_id"], ["logistics_transfer.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["resolved_by_user_id"], ["logistics_user.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_logistics_manual_review_status_type",
        "logistics_manual_review",
        ["status", "review_type"],
        unique=False,
    )
    op.create_index(
        "ix_logistics_manual_review_transfer",
        "logistics_manual_review",
        ["transfer_id"],
        unique=False,
    )
    op.create_index(
        "ix_logistics_manual_review_source",
        "logistics_manual_review",
        ["source_document_type", "source_external_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_logistics_manual_review_source", table_name="logistics_manual_review")
    op.drop_index("ix_logistics_manual_review_transfer", table_name="logistics_manual_review")
    op.drop_index("ix_logistics_manual_review_status_type", table_name="logistics_manual_review")
    op.drop_table("logistics_manual_review")

    op.drop_index("ix_logistics_route_run_item_dropoff", table_name="logistics_route_run_item")
    op.drop_index("ix_logistics_route_run_item_transfer", table_name="logistics_route_run_item")
    op.drop_table("logistics_route_run_item")

    if _is_postgresql():
        op.drop_constraint("fk_logistics_draft_route_run", "logistics_draft", type_="foreignkey")
    op.drop_column("logistics_draft", "route_run_id")

    op.drop_index("ix_logistics_route_run_status_planned", table_name="logistics_route_run")
    op.drop_table("logistics_route_run")

    op.drop_index("ix_logistics_transfer_site_order_number", table_name="logistics_transfer")
    op.drop_index("ix_logistics_transfer_lookup_code", table_name="logistics_transfer")
    if _is_postgresql():
        op.drop_constraint(
            "uq_logistics_transfer_source_external",
            "logistics_transfer",
            type_="unique",
        )
        op.drop_constraint(
            "fk_logistics_transfer_document_target_warehouse",
            "logistics_transfer",
            type_="foreignkey",
        )
        op.create_unique_constraint(
            "logistics_transfer_external_id_key",
            "logistics_transfer",
            ["external_id"],
        )
    op.drop_column("logistics_transfer", "site_order_number")
    op.drop_column("logistics_transfer", "origin_order_external_id")
    op.drop_column("logistics_transfer", "document_target_warehouse_id")
    op.drop_column("logistics_transfer", "lookup_code")
    op.drop_column("logistics_transfer", "source_document_type")
