from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "e2f3a4b5c6d7"
down_revision: str | None = "d1e2f3a4b5c6"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "site_order_execution_case",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("site_order_number", sa.String(length=32), nullable=False),
        sa.Column("bitrix_deal_id", sa.Integer(), nullable=True),
        sa.Column("onec_order_external_id", sa.String(length=64), nullable=True),
        sa.Column("rtu_external_id", sa.String(length=64), nullable=True),
        sa.Column("delivery_method", sa.String(length=64), nullable=True),
        sa.Column("raw_delivery_method", sa.String(length=255), nullable=True),
        sa.Column("payment_status", sa.String(length=64), nullable=True),
        sa.Column("current_derived_status", sa.String(length=64), nullable=False),
        sa.Column("current_crm_stage", sa.String(length=64), nullable=True),
        sa.Column("confidence", sa.String(length=32), nullable=True),
        sa.Column("last_evidence_event_id", sa.Integer(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("site_order_number", name="uq_site_order_execution_case_order"),
    )
    op.create_index(
        "ix_site_order_execution_case_delivery",
        "site_order_execution_case",
        ["delivery_method"],
    )
    op.create_index(
        "ix_site_order_execution_case_status",
        "site_order_execution_case",
        ["current_derived_status"],
    )

    op.create_table(
        "bitrix_chat_message",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("chat_code", sa.String(length=64), nullable=False),
        sa.Column("dialog_id", sa.String(length=64), nullable=False),
        sa.Column("chat_id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=False),
        sa.Column("message_at", sa.DateTime(), nullable=True),
        sa.Column("author_id", sa.String(length=64), nullable=True),
        sa.Column("raw_text_hash", sa.String(length=64), nullable=True),
        sa.Column("raw_text_redacted", sa.Text(), nullable=True),
        sa.Column("parser_version", sa.String(length=32), nullable=False),
        sa.Column("parse_status", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("chat_id", "message_id", name="uq_bitrix_chat_message_identity"),
    )
    op.create_index(
        "ix_bitrix_chat_message_chat_code_at",
        "bitrix_chat_message",
        ["chat_code", "message_at"],
    )

    op.create_table(
        "bitrix_chat_mention",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=False),
        sa.Column("site_order_number", sa.String(length=32), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("confidence", sa.String(length=32), nullable=False),
        sa.Column("evidence_text", sa.String(length=1000), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["message_id"], ["bitrix_chat_message.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "message_id",
            "site_order_number",
            "event_type",
            name="uq_bitrix_chat_mention_order_event",
        ),
    )
    op.create_index(
        "ix_bitrix_chat_mention_order",
        "bitrix_chat_mention",
        ["site_order_number"],
    )

    op.create_table(
        "site_order_execution_event",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("case_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("event_at", sa.DateTime(), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("source_ref", sa.String(length=255), nullable=True),
        sa.Column("confidence", sa.String(length=32), nullable=False),
        sa.Column("raw_message_id", sa.Integer(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["case_id"],
            ["site_order_execution_case.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["raw_message_id"],
            ["bitrix_chat_message.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_site_order_execution_event_idempotency",
        ),
    )
    op.create_index(
        "ix_site_order_execution_event_case_at",
        "site_order_execution_event",
        ["case_id", "event_at"],
    )
    op.create_index(
        "ix_site_order_execution_event_type",
        "site_order_execution_event",
        ["event_type"],
    )


def downgrade() -> None:
    op.drop_index("ix_site_order_execution_event_type", table_name="site_order_execution_event")
    op.drop_index("ix_site_order_execution_event_case_at", table_name="site_order_execution_event")
    op.drop_table("site_order_execution_event")
    op.drop_index("ix_bitrix_chat_mention_order", table_name="bitrix_chat_mention")
    op.drop_table("bitrix_chat_mention")
    op.drop_index("ix_bitrix_chat_message_chat_code_at", table_name="bitrix_chat_message")
    op.drop_table("bitrix_chat_message")
    op.drop_index("ix_site_order_execution_case_status", table_name="site_order_execution_case")
    op.drop_index("ix_site_order_execution_case_delivery", table_name="site_order_execution_case")
    op.drop_table("site_order_execution_case")
