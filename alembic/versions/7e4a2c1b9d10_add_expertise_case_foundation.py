"""add expertise case foundation

Revision ID: 7e4a2c1b9d10
Revises: 0a12bc34de56, 1f4d6e8c9b0a
Create Date: 2026-04-02 22:40:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7e4a2c1b9d10"
down_revision: str | Sequence[str] | None = ("0a12bc34de56", "1f4d6e8c9b0a")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "expertise_case",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("external_id", sa.String(length=64), nullable=False),
        sa.Column("onec_expertise_ref", sa.String(length=64), nullable=True),
        sa.Column("onec_expertise_number", sa.String(length=64), nullable=True),
        sa.Column("created_at_source", sa.DateTime(), nullable=True),
        sa.Column("store_external_id", sa.String(length=64), nullable=True),
        sa.Column("store_name", sa.String(length=255), nullable=True),
        sa.Column("customer_name", sa.String(length=255), nullable=True),
        sa.Column("customer_phone", sa.String(length=64), nullable=True),
        sa.Column("problem_summary", sa.String(length=1000), nullable=True),
        sa.Column("current_status", sa.String(length=32), nullable=False),
        sa.Column("decision_code", sa.String(length=64), nullable=True),
        sa.Column("decision_comment", sa.String(length=1000), nullable=True),
        sa.Column("client_notified", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("due_at", sa.DateTime(), nullable=True),
        sa.Column("owner_user_external_id", sa.String(length=64), nullable=True),
        sa.Column("bitrix_entity_id", sa.String(length=64), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("external_id", name="uq_expertise_case_external_id"),
        sa.UniqueConstraint("onec_expertise_ref", name="uq_expertise_case_onec_expertise_ref"),
    )
    op.create_index(
        "ix_expertise_case_current_status",
        "expertise_case",
        ["current_status"],
        unique=False,
    )
    op.create_index(
        "ix_expertise_case_store_external_id",
        "expertise_case",
        ["store_external_id"],
        unique=False,
    )
    op.create_index(
        "ix_expertise_case_owner_user_external_id",
        "expertise_case",
        ["owner_user_external_id"],
        unique=False,
    )
    op.create_index("ix_expertise_case_due_at", "expertise_case", ["due_at"], unique=False)
    op.create_index(
        "ix_expertise_case_client_notified",
        "expertise_case",
        ["client_notified"],
        unique=False,
    )

    op.create_table(
        "expertise_case_event",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("expertise_case_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("event_at", sa.DateTime(), nullable=False),
        sa.Column("actor_external_id", sa.String(length=64), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("comment", sa.String(length=1000), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["expertise_case_id"], ["expertise_case.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_expertise_case_event_idempotency_key"),
    )
    op.create_index(
        "ix_expertise_case_event_case_event_at",
        "expertise_case_event",
        ["expertise_case_id", "event_at"],
        unique=False,
    )
    op.create_index(
        "ix_expertise_case_event_event_type",
        "expertise_case_event",
        ["event_type"],
        unique=False,
    )

    op.create_table(
        "expertise_case_attachment",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("expertise_case_id", sa.Integer(), nullable=False),
        sa.Column("attachment_kind", sa.String(length=32), nullable=False),
        sa.Column("storage_ref", sa.String(length=255), nullable=False),
        sa.Column("comment", sa.String(length=1000), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["expertise_case_id"], ["expertise_case.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_expertise_case_attachment_case_id",
        "expertise_case_attachment",
        ["expertise_case_id"],
        unique=False,
    )
    op.create_index(
        "ix_expertise_case_attachment_kind",
        "expertise_case_attachment",
        ["attachment_kind"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_expertise_case_attachment_kind", table_name="expertise_case_attachment")
    op.drop_index("ix_expertise_case_attachment_case_id", table_name="expertise_case_attachment")
    op.drop_table("expertise_case_attachment")
    op.drop_index("ix_expertise_case_event_event_type", table_name="expertise_case_event")
    op.drop_index("ix_expertise_case_event_case_event_at", table_name="expertise_case_event")
    op.drop_table("expertise_case_event")
    op.drop_index("ix_expertise_case_client_notified", table_name="expertise_case")
    op.drop_index("ix_expertise_case_due_at", table_name="expertise_case")
    op.drop_index("ix_expertise_case_owner_user_external_id", table_name="expertise_case")
    op.drop_index("ix_expertise_case_store_external_id", table_name="expertise_case")
    op.drop_index("ix_expertise_case_current_status", table_name="expertise_case")
    op.drop_table("expertise_case")
