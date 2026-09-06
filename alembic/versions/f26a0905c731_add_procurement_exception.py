"""Persistent procurement exceptions and reaction deadlines."""

import sqlalchemy as sa

from alembic import op

revision = "f26a0905c731"
down_revision = "a9b0c1d2e3f4"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "procurement_exception",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("stable_key", sa.String(255), nullable=False, unique=True),
        sa.Column(
            "order_id",
            sa.Integer(),
            sa.ForeignKey("procurement_order_formation.id"),
            nullable=False,
        ),
        sa.Column("line_id", sa.Integer(), sa.ForeignKey("procurement_order_formation_line.id")),
        sa.Column("reason_code", sa.String(80), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("facts_hash", sa.String(64), nullable=False),
        sa.Column("facts", sa.JSON(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("response_due_at", sa.DateTime(), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime()),
        sa.Column("assigned_user_id", sa.String(64)),
        sa.Column("next_action", sa.Text()),
        sa.Column("next_action_due_at", sa.DateTime()),
        sa.Column("resolution", sa.Text()),
        sa.Column("resolved_facts_hash", sa.String(64)),
        sa.Column("resolved_at", sa.DateTime()),
    )
    op.create_index(
        "ix_procurement_exception_status_due",
        "procurement_exception",
        ["status", "response_due_at"],
    )


def downgrade():
    # A schema rollback must never erase employee decisions.
    raise RuntimeError("Procurement exception history must be retained; roll back application only")
