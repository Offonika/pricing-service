from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "7b4d9a2e6c31"
down_revision: str | None = "3e7a9c1d5f24"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "bitrix_bot_input_session",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("dialog_id", sa.String(length=64), nullable=False),
        sa.Column("actor_id", sa.String(length=64), nullable=False),
        sa.Column("interaction", sa.String(length=32), nullable=False),
        sa.Column("source_message_id", sa.String(length=64), nullable=False),
        sa.Column("consumed_message_id", sa.String(length=64), nullable=True),
        sa.Column("prompt_message_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "dialog_id",
            "actor_id",
            "source_message_id",
            name="uq_bitrix_bot_input_session_source",
        ),
    )
    op.create_index(
        "ix_bitrix_bot_input_session_actor_status_expires",
        "bitrix_bot_input_session",
        ["dialog_id", "actor_id", "status", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_bitrix_bot_input_session_actor_status_expires",
        table_name="bitrix_bot_input_session",
    )
    op.drop_table("bitrix_bot_input_session")
