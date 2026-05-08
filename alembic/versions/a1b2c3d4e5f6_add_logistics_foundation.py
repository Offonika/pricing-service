"""add logistics foundation

Revision ID: a1b2c3d4e5f6
Revises: 2cc3d4e5f6a7, fe45ab67cd89
Create Date: 2026-03-28 22:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: str | Sequence[str] | None = ("2cc3d4e5f6a7", "fe45ab67cd89")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "logistics_warehouse",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("external_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("external_id"),
    )

    op.create_table(
        "logistics_driver",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("external_id", sa.String(length=64), nullable=True),
        sa.Column("full_name", sa.String(length=255), nullable=False),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("external_id"),
    )

    op.create_table(
        "logistics_user",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("external_id", sa.String(length=64), nullable=True),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=True),
        sa.Column("username", sa.String(length=255), nullable=True),
        sa.Column("full_name", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("default_warehouse_id", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["default_warehouse_id"], ["logistics_warehouse.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("external_id"),
        sa.UniqueConstraint("telegram_user_id"),
    )

    op.create_table(
        "logistics_transfer",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("external_id", sa.String(length=64), nullable=False),
        sa.Column("document_number", sa.String(length=64), nullable=False),
        sa.Column("document_date", sa.DateTime(), nullable=False),
        sa.Column("source_warehouse_id", sa.Integer(), nullable=False),
        sa.Column("target_warehouse_id", sa.Integer(), nullable=False),
        sa.Column("final_recipient_name", sa.String(length=255), nullable=True),
        sa.Column("barcode", sa.String(length=255), nullable=False),
        sa.Column("onec_status", sa.String(length=64), nullable=True),
        sa.Column("onec_deleted", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_warehouse_id"], ["logistics_warehouse.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["target_warehouse_id"], ["logistics_warehouse.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("external_id"),
        sa.UniqueConstraint("barcode"),
    )
    op.create_index(
        "ix_logistics_transfer_barcode", "logistics_transfer", ["barcode"], unique=False
    )

    op.create_table(
        "logistics_transfer_state",
        sa.Column("transfer_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("current_warehouse_id", sa.Integer(), nullable=True),
        sa.Column("dropoff_warehouse_id", sa.Integer(), nullable=True),
        sa.Column("driver_id", sa.Integer(), nullable=True),
        sa.Column("last_event_type", sa.String(length=64), nullable=False),
        sa.Column("last_event_at", sa.DateTime(), nullable=False),
        sa.Column("last_user_id", sa.Integer(), nullable=True),
        sa.Column("last_document_ref", sa.String(length=64), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["current_warehouse_id"], ["logistics_warehouse.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["dropoff_warehouse_id"], ["logistics_warehouse.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["driver_id"], ["logistics_driver.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["last_user_id"], ["logistics_user.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["transfer_id"], ["logistics_transfer.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("transfer_id"),
    )
    op.create_index(
        "ix_logistics_transfer_state_status_current",
        "logistics_transfer_state",
        ["status", "current_warehouse_id"],
        unique=False,
    )
    op.create_index(
        "ix_logistics_transfer_state_status_dropoff",
        "logistics_transfer_state",
        ["status", "dropoff_warehouse_id"],
        unique=False,
    )

    op.create_table(
        "logistics_transfer_event",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("transfer_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("event_at", sa.DateTime(), nullable=False),
        sa.Column("warehouse_id", sa.Integer(), nullable=True),
        sa.Column("dropoff_warehouse_id", sa.Integer(), nullable=True),
        sa.Column("driver_id", sa.Integer(), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("comment", sa.String(length=1000), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("document_ref", sa.String(length=64), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["dropoff_warehouse_id"], ["logistics_warehouse.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["driver_id"], ["logistics_driver.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["transfer_id"], ["logistics_transfer.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["logistics_user.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["warehouse_id"], ["logistics_warehouse.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_logistics_transfer_event_idempotency_key"),
    )
    op.create_index(
        "ix_logistics_transfer_event_transfer_event_at",
        "logistics_transfer_event",
        ["transfer_id", "event_at"],
        unique=False,
    )

    op.create_table(
        "logistics_event_photo",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("telegram_file_id", sa.String(length=255), nullable=False),
        sa.Column("file_kind", sa.String(length=32), server_default="photo", nullable=False),
        sa.Column("comment", sa.String(length=1000), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["logistics_transfer_event.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "logistics_draft",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("draft_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="open", nullable=False),
        sa.Column("warehouse_id", sa.Integer(), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=False),
        sa.Column("driver_id", sa.Integer(), nullable=True),
        sa.Column("default_dropoff_warehouse_id", sa.Integer(), nullable=True),
        sa.Column("comment", sa.String(length=1000), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["actor_user_id"], ["logistics_user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["default_dropoff_warehouse_id"], ["logistics_warehouse.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["driver_id"], ["logistics_driver.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["warehouse_id"], ["logistics_warehouse.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "logistics_draft_item",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("draft_id", sa.Integer(), nullable=False),
        sa.Column("transfer_id", sa.Integer(), nullable=False),
        sa.Column("barcode", sa.String(length=255), nullable=False),
        sa.Column("dropoff_warehouse_id", sa.Integer(), nullable=True),
        sa.Column("scan_user_id", sa.Integer(), nullable=True),
        sa.Column("scan_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["draft_id"], ["logistics_draft.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["dropoff_warehouse_id"], ["logistics_warehouse.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["scan_user_id"], ["logistics_user.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["transfer_id"], ["logistics_transfer.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("draft_id", "transfer_id", name="uq_logistics_draft_item_transfer"),
    )

    op.create_table(
        "logistics_bot_session",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=False),
        sa.Column("draft_id", sa.Integer(), nullable=False),
        sa.Column("draft_type", sa.String(length=32), nullable=False),
        sa.Column("status_message_id", sa.BigInteger(), nullable=True),
        sa.Column("scan_error_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("recent_errors", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["actor_user_id"], ["logistics_user.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["draft_id"], ["logistics_draft.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("chat_id", name="uq_logistics_bot_session_chat_id"),
    )

    op.create_table(
        "logistics_bot_session_photo",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("telegram_file_id", sa.String(length=255), nullable=False),
        sa.Column("comment", sa.String(length=1000), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["logistics_bot_session.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("logistics_bot_session_photo")
    op.drop_table("logistics_bot_session")
    op.drop_table("logistics_draft_item")
    op.drop_table("logistics_draft")
    op.drop_table("logistics_event_photo")
    op.drop_index(
        "ix_logistics_transfer_event_transfer_event_at", table_name="logistics_transfer_event"
    )
    op.drop_table("logistics_transfer_event")
    op.drop_index(
        "ix_logistics_transfer_state_status_dropoff", table_name="logistics_transfer_state"
    )
    op.drop_index(
        "ix_logistics_transfer_state_status_current", table_name="logistics_transfer_state"
    )
    op.drop_table("logistics_transfer_state")
    op.drop_index("ix_logistics_transfer_barcode", table_name="logistics_transfer")
    op.drop_table("logistics_transfer")
    op.drop_table("logistics_user")
    op.drop_table("logistics_driver")
    op.drop_table("logistics_warehouse")
